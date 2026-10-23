const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function element() {
    return { style: {}, children: [], appendChild(child) { this.children.push(child); } };
}

function loadApp(history, storage = new Map(), fetchOverride = null) {
    const context = {
        console,
        TextDecoder,
        localStorage: { getItem(key) { return storage.get(key) ?? null; },
            setItem(key, value) { storage.set(key, value); } },
        document: { createElement: element, head: element(), addEventListener() {} },
        fetch: fetchOverride || (async () => ({ ok: true, json: async () => ({ history }) })),
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8') +
        '\nglobalThis.App = AIOnCallApp;', context);
    return Object.create(context.App.prototype);
}

function diagnosisStream(chunks) {
    let position = 0;
    const app = loadApp([], new Map(), async () => ({ok: true, body: {getReader: () => ({
        async read() { return position < chunks.length
            ? {done: false, value: new TextEncoder().encode(chunks[position++])} : {done: true}; },
        releaseLock() {},
    })}}));
    Object.assign(app, {getRequestHeaders: value => value, ensureAuthenticated() {},
        updateAIOpsMessage() {}, updateAIOpsStreamContent() {}});
    return app;
}

test('diagnosis terminal errors propagate regardless of their message wording', async () => {
    for (const payload of [
        {type: 'error', data: 'provider offline'},
        {type: 'error', stage: 'dead_lettered', message: 'retries exhausted'},
    ]) {
        const app = diagnosisStream([`data: ${JSON.stringify(payload)}\n\n`]);
        await assert.rejects(app.sendAIOpsRequest({}), /provider offline|retries exhausted/);
    }
});

test('diagnosis truncated stream is not treated as successful completion', async () => {
    const app = diagnosisStream(['data: {"type":"status","message":"queued"}\n\n']);
    await assert.rejects(app.sendAIOpsRequest({}), /连接.*中断/);
});

test('diagnosis retry progress can continue to an explicit completion', async () => {
    const app = diagnosisStream([
        'data: {"type":"status","stage":"attempt_failed","message":"retrying"}\n\n',
        'data: {"type":"complete","response":"final report"}\n\n',
    ]);
    await app.sendAIOpsRequest({});
});

test('feedback submits the chosen resolution and tolerates request failure', async () => {
    let sent;
    let ok = true;
    const app = loadApp([], new Map(), async (url, options) => {
        sent = JSON.parse(options.body);
        return {ok};
    });
    app.currentUser = {id: 1};
    app.getRequestHeaders = headers => headers;
    app.ensureAuthenticated = () => {};
    const message = element();
    app.attachFeedback(message, 'interaction-one');
    const controls = message.children[0];
    assert.equal(controls.children.length, 4);
    assert.equal(controls.children[1].textContent, '已解决');
    await controls.children[1].onclick();
    assert.equal(sent.interaction_id, 'interaction-one');
    assert.equal(sent.resolution, 'resolved');
    assert.equal(controls.children[0].textContent, '反馈已保存，可重新选择');
    ok = false;
    await controls.children[2].onclick();
    assert.equal(controls.children[0].textContent, '反馈未保存，请稍后重试');
});

test('empty user feedback summary never displays a fabricated resolution rate', async () => {
    const app = loadApp([], new Map(), async () => ({ok: true, json: async () => ({
        interactions: 0, distinct_human_users: 0, feedback_count: 0, resolved_rate: null,
        feedback_coverage: null, latency_p95_ms: null
    })}));
    app.ensureAuthenticated = () => {};
    let output;
    app.addMessage = (role, text) => {output = text;};
    await app.showUsageSummary();
    assert.match(output, /暂无反馈/);
    assert.doesNotMatch(output, /100%/);
});

test('server history retains user/assistant roles and current history', async () => {
    const messages = [
        { role: 'user', content: 'Show a list' },
        { role: 'assistant', content: '- first\n- second' },
    ];
    const app = loadApp(messages);
    Object.assign(app, {
        chatHistories: [{ id: 'saved', messages: [] }], currentChatHistory: [],
        chatMessages: element(), rendered: [],
        getRequestHeaders: () => ({}), ensureAuthenticated() {},
        addMessage(type, content) { this.rendered.push({ type, content }); },
        checkAndSetCentered() {}, renderChatHistory() {},
    });
    await app.loadChatHistory('saved');
    assert.equal(app.rendered[1].type, 'assistant');
    assert.equal(app.currentChatHistory.length, 2);
    assert.equal(app.currentChatHistory[1].content, messages[1].content);
    assert.equal(app.currentChatHistory[1].type, 'assistant');
});

test('legacy cached bot messages use the sanitized Markdown renderer', () => {
    const app = loadApp([]);
    let rendered;
    Object.assign(app, {
        chatMessages: { querySelectorAll: () => [], appendChild() {} },
        currentChatHistory: [], scrollToBottom() {}, highlightCodeBlocks() {},
        renderMarkdown(content) { rendered = content; return '<ul><li>safe</li></ul>'; },
    });
    const message = app.addMessage('bot', '- safe', false, false);
    assert.equal(rendered, '- safe');
    assert.equal(message.className, 'message assistant');
});


test('new streaming chat survives reload without switching sessions', () => {
    const app = loadApp([]);
    let persisted;
    Object.assign(app, {sessionId: 'new-session', chatHistories: [],
        currentChatHistory: [{type: 'user', content: 'question'}],
        isCurrentChatFromHistory: false, renderChatHistory() {},
        saveChatHistories() { persisted = JSON.stringify(this.chatHistories); }});
    app.handleStreamComplete(null, 'answer');
    assert.equal(JSON.parse(persisted)[0].id, 'new-session');
    assert.equal(JSON.parse(persisted)[0].messages[1].content, 'answer');
    app.handleStreamComplete(null, 'second answer');
    assert.equal(JSON.parse(persisted).length, 1);
});


test('first user message is restored from local storage before any reply', () => {
    const storage = new Map();
    const app = loadApp([], storage);
    Object.assign(app, {sessionId: 'pending', historyStorageKey: 'user-7',
        currentChatHistory: [], chatHistories: [], scrollToBottom() {},
        chatMessages: {querySelectorAll: () => [], appendChild() {}}});
    app.addMessage('user', 'pending question');
    const reloaded = loadApp([], storage);
    reloaded.historyStorageKey = 'user-7';
    assert.equal(reloaded.loadChatHistories()[0].messages[0].content, 'pending question');
    reloaded.historyStorageKey = 'user-8';
    assert.equal(reloaded.loadChatHistories().length, 0);
});
