const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {SSEParser, DiagnosisClient, progressText} = fs.existsSync(`${__dirname}/../static/diagnosis-stream.js`)
    ? require('../static/diagnosis-stream.js') : {};

const encode = text => new TextEncoder().encode(text);
const frame = (id, data) => `id: ${id}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
const response = (...chunks) => new Response(new ReadableStream({start(c) {
    for (const chunk of chunks) c.enqueue(encode(chunk));
    c.close();
}}), {headers: {'Content-Type': 'text/event-stream'}});
function storage() {
    const values = new Map();
    return {get length() { return values.size; }, key: i => [...values.keys()][i] || null,
        getItem: k => values.get(k) || null, setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k)};
}

test('parser preserves UTF8, multiline data, comments and split CRLF; drops truncated frames', () => {
    assert.equal(typeof SSEParser, 'function', 'SSE parser is available');
    const events = [];
    const parser = new SSEParser(event => events.push(event));
    const bytes = encode(': heartbeat\r\n\r\nid: 7\r\nevent: message\r\ndata: {"type":"status",\r\ndata: "message":"中文"}\r\n\r\nid: 8\ndata: {"type":"complete"}');
    for (const byte of bytes) parser.push(Uint8Array.of(byte));
    parser.end();
    assert.equal(events.length, 1);
    assert.equal(events[0].id, '7');
    assert.equal(JSON.parse(events[0].data).message, '中文');
});

test('a final CR is a real frame boundary while unterminated data is discarded', () => {
    const events = [];
    const parser = new SSEParser(event => events.push(event));
    parser.push(encode('id: 1\rdata: {"type":"complete"}\r\r'));
    parser.end();
    assert.equal(events.length, 1);
});

test('frames after an authoritative terminal event cannot regress the displayed result', async () => {
    let subscriptions = 0;
    const client = new DiagnosisClient({userId: 'alice', storage: storage(), fetch: async (_url, options) => {
        if (options.method !== 'POST') subscriptions++;
        return options.method === 'POST' ? Response.json({diagnosis_id: 'diag', events_url: '/api/aiops/jobs/diag/events'}, {status: 202})
        : response(frame('terminal:2:completed', {type: 'complete', response: 'authoritative report'}) +
                   frame(99, {type: 'report', report: 'late old report'})); }});
    await client.start({});
    assert.equal(client.state.progress.report, 'authoritative report');
    assert.equal(client.state.lastEventId, 'terminal:2:completed');
    assert.equal(subscriptions, 1);
});

test('interrupted frame reconnects from last applied ID, deduplicates and displays final report once', async () => {
    assert.equal(typeof DiagnosisClient, 'function', 'Durable diagnosis client is available');
    const calls = [], delays = [], updates = [];
    const client = new DiagnosisClient({userId: 'alice', storage: storage(), keyFactory: () => 'create-key',
        sleep: async ms => delays.push(ms), onUpdate: s => updates.push(s.lastEventId),
        fetch: async (url, options) => {
            calls.push([url, options]);
            if (options.method === 'POST') return Response.json({diagnosis_id: 'diag-1', events_url: '/api/aiops/jobs/diag-1/events'}, {status: 202});
            if (calls.length === 2) return response(frame(1, {type: 'status', message: 'working'}), 'id: 2\ndata: {"type":"report",');
            return response(frame(1, {type: 'status', message: 'working'}), frame(2, {type: 'report', report: 'unique report'}),
                frame('terminal:1:completed', {type: 'complete', response: 'unique report'}));
        }});
    await client.start({session_id: 'session-1'});
    assert.equal(calls.filter(([, o]) => o.method === 'POST').length, 1);
    assert.equal(calls[2][1].headers['Last-Event-ID'], '1');
    assert.deepEqual(delays, [1000]);
    assert.equal(progressText(client.state).split('unique report').length - 1, 1);
    assert.equal(progressText(client.state).split('working').length - 1, 1);
    assert.equal(client.state.lastEventId, 'terminal:1:completed');
    assert.equal(client.state.done, true);
});

test('uncertain create reuses persisted idempotency key; refresh resumes existing task without POST', async () => {
    const cache = storage(), keys = [], calls = [];
    let client;
    client = new DiagnosisClient({userId: 'alice', storage: cache, keyFactory: () => 'stable-key', sleep: async () => {},
        fetch: async (_url, options) => {
            keys.push(options.headers['Idempotency-Key']);
            if (keys.length === 1) throw new Error('lost creation response');
            assert.equal(JSON.parse(cache.getItem('oncallDiagnosis:user:alice')).idempotencyKey, 'stable-key');
            client.stop();
            return Response.json({diagnosis_id: 'diag-refresh', events_url: '/api/aiops/jobs/diag-refresh/events'}, {status: 202});
        }});
    await assert.rejects(client.start({session_id: 'same'}), {name: 'AbortError'});
    assert.deepEqual(keys, ['stable-key', 'stable-key']);
    // Simulate reload while a complete progress event is already saved.
    const saved = {...client.state, diagnosisId: 'diag-refresh', eventsUrl: '/api/aiops/jobs/diag-refresh/events',
                   lastEventId: '12', lastEvidenceId: '12', progress: {log: 'saved progress', report: ''}};
    cache.setItem('oncallDiagnosis:user:alice', JSON.stringify(saved));
    const refreshed = new DiagnosisClient({userId: 'alice', storage: cache, fetch: async (url, options) => {
        calls.push([url, options]);
        return response(frame('terminal:1:completed', {type: 'complete', response: 'restored report'}));
    }});
    await refreshed.resume();
    assert.equal(calls.length, 1);
    assert.equal(calls[0][1].headers['Last-Event-ID'], '12');
    assert.match(progressText(refreshed.state), /saved progress/);
    assert.match(progressText(refreshed.state), /restored report/);
});

test('refresh after unknown create result uses original payload and key', async () => {
    const cache = storage();
    cache.setItem('oncallDiagnosis:user:alice', JSON.stringify({userId: 'alice', idempotencyKey: 'original',
        payload: {session_id: 'original-session'}, lastEventId: '', progress: {log: '', report: ''}}));
    const calls = [];
    const client = new DiagnosisClient({userId: 'alice', storage: cache, fetch: async (url, options) => {
        calls.push(options);
        return options.method === 'POST'
            ? Response.json({diagnosis_id: 'diag', events_url: '/api/aiops/jobs/diag/events'}, {status: 202})
            : response(frame('terminal:0:completed', {type: 'complete', response: 'done'}));
    }});
    await client.resume();
    assert.equal(calls[0].headers['Idempotency-Key'], 'original');
    assert.equal(JSON.parse(calls[0].body).session_id, 'original-session');
});

test('network retry exponential delays capped at 30 seconds; authorization failure stops', async () => {
    const delays = [];
    let count = 0;
    const client = new DiagnosisClient({userId: 'alice', storage: storage(), sleep: async ms => delays.push(ms),
        fetch: async () => { if (++count < 9) throw new Error('offline'); return new Response('', {status: 403}); }});
    await assert.rejects(client.start({}), /403/);
    assert.deepEqual(delays, [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
    assert.equal(count, 9);
    for (const status of [401, 403]) {
        let attempts = 0;
        const stopped = new DiagnosisClient({userId: 'alice', storage: storage(), fetch: async () => {
            attempts++; return new Response('', {status});
        }});
        await assert.rejects(stopped.start({}), new RegExp(String(status)));
        assert.equal(attempts, 1);
    }
});

test('account switch aborts active fetch, erases previous state and cannot apply late events', async () => {
    const cache = storage();
    let signal, resolve;
    const client = new DiagnosisClient({userId: 'alice', storage: cache, fetch: async (_url, options) => {
        signal = options.signal;
        return new Promise(r => { resolve = r; });
    }});
    const run = client.start({});
    client.setUser('bob');
    assert.equal(signal.aborted, true);
    resolve(Response.json({diagnosis_id: 'secret', events_url: '/api/aiops/jobs/secret/events'}, {status: 202}));
    await assert.rejects(run, {name: 'AbortError'});
    assert.equal(cache.getItem('oncallDiagnosis:user:alice'), null);
    assert.equal(client.state, null);
});

test('real AI Ops entrypoint creates durable task and renders report once in existing UI', async () => {
    const source = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
    const context = {window: {addEventListener() {}}, document: {addEventListener() {}},
        console, AbortController, TextDecoder, DiagnosisStream: {DiagnosisClient, progressText}, localStorage: storage()};
    vm.createContext(context);
    vm.runInContext(source.slice(0, source.indexOf('\nconst style =')) + '\nthis.App = AIOnCallApp;', context);
    const app = Object.create(context.App.prototype);
    const element = {}, rendered = [];
    Object.assign(app, {currentUser: {id: 'alice'}, sessionId: 'ui-session', apiBaseUrl: '/api',
        isIdentityCurrent: () => true, getRequestHeaders: h => h, updateAIOpsStreamContent: (_e, text) => rendered.push(text),
        updateAIOpsMessage: (_e, text) => rendered.push(text), updateUI() {},
        createDiagnosisClient: context.App.prototype.createDiagnosisClient});
    context.fetch = async (_url, options) => options.method === 'POST'
        ? Response.json({diagnosis_id: 'ui-task', events_url: '/api/aiops/jobs/ui-task/events'}, {status: 202})
        : response(frame(1, {type: 'report', report: 'UI report'}), frame('terminal:1:completed', {type: 'complete', response: 'UI report'}));
    await app.sendAIOpsRequest(element);
    assert.match(rendered.at(-1), /UI report/);
    assert.equal(rendered.at(-1).split('UI report').length - 1, 1);
});

test('a fresh app after account change removes the previous account task cache', () => {
    const cache = storage();
    cache.setItem('aiOnCallAuthState', JSON.stringify({userId: 'bob', version: 'after-login'}));
    cache.setItem('oncallDiagnosis:user:alice', '{"diagnosisId":"private-task"}');
    const context = {console, localStorage: cache, window: {addEventListener() {}}, document: {addEventListener() {}}};
    vm.createContext(context);
    const source = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
    vm.runInContext(source.slice(0, source.indexOf('\nconst style =')) + '\nthis.App = AIOnCallApp;', context);
    const app = Object.create(context.App.prototype);
    app.currentUser = {id: 'bob'};
    app.initializeIdentity();
    assert.equal(cache.getItem('oncallDiagnosis:user:alice'), null);
});

test('evidence and skipped steps remain readable without rendering raw tool payloads', async () => {
    const client = new DiagnosisClient({userId: 'alice', storage: storage(), fetch: async (_url, options) =>
        options.method === 'POST'
            ? Response.json({diagnosis_id: 'diag', events_url: '/api/aiops/jobs/diag/events'}, {status: 202})
            : response(frame(1, {type: 'evidence', summary: '已查到零条告警', result: {raw: 'PRIVATE_RAW_TOOL'}}),
                frame(2, {type: 'report', report: '没有活动告警', skipped_steps: ['指标', '日志']}),
                frame('terminal:1:completed', {type: 'complete', response: '没有活动告警'}))});
    await client.start({});
    const text = progressText(client.state);
    assert.match(text, /已查到零条告警/);
    assert.match(text, /2.*步骤.*跳过|跳过.*2.*步骤/);
    assert.doesNotMatch(text, /PRIVATE_RAW_TOOL/);
    assert.equal(text.split('没有活动告警').length - 1, 1);
});
