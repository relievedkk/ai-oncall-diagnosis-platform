const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function element() {
    return { style: {}, children: [], appendChild(child) { this.children.push(child); } };
}

function loadApp(history, storage = new Map(), fetchOverride = null) {
    const listeners = {};
    const context = {
        console,
        TextDecoder,
        AbortController,
        window: {addEventListener(name, callback) {listeners[name] = callback;}, location: {replace() {}}},
        localStorage: { getItem(key) { return storage.get(key) ?? null; },
            setItem(key, value) { storage.set(key, value); },
            removeItem(key) {storage.delete(key);} },
        document: { createElement: element, head: element(),
            addEventListener(name, callback) {listeners[`document:${name}`] = callback;} },
        fetch: fetchOverride || (async () => ({ ok: true, json: async () => ({ history }) })),
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8') +
        '\nglobalThis.App = AIOnCallApp;', context);
    const app = Object.create(context.App.prototype);
    app.testListeners = listeners;
    app.testContext = context;
    return app;
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

test('account change while history JSON loads cannot restore old cached messages', async () => {
    const storage = new Map();
    let resolveJson;
    const app = loadApp([], storage, async () => ({ok: true,
        json: () => new Promise(resolve => {resolveJson = resolve;})}));
    Object.assign(app, {currentUser: {id: 1}, historyStorageKey: 'chatHistories:user:1',
        chatHistories: [{id: 'old', messages: [{type: 'user', content: 'private A'}]}],
        currentChatHistory: [], chatMessages: element(), rendered: [],
        getRequestHeaders: () => ({}),
        addMessage(type, content) {this.rendered.push(content);},
        checkAndSetCentered() {}, renderChatHistory() {}});
    app.initializeIdentity();
    const pending = app.loadChatHistory('old');
    while (!resolveJson) await Promise.resolve();
    storage.set('aiOnCallAuthState', JSON.stringify({userId: 2, version: 'new-login'}));
    resolveJson({history: []});
    await pending;
    assert.equal(app.rendered.length, 0);
    assert.equal(app.currentChatHistory.length, 0);
    assert.equal(app.identityInvalidated, true);
});

test('logout clears previous user cache and blocks stale tab requests', async () => {
    const storage = new Map([['chatHistories:user:1', 'private cache']]);
    let requests = 0;
    const fetcher = async () => {requests++; return {ok: true};};
    const app = loadApp([], storage, fetcher);
    const stale = loadApp([], storage, fetcher);
    for (const instance of [app, stale]) {
        Object.assign(instance, {currentUser: {id: 1}, historyStorageKey: 'chatHistories:user:1',
            chatHistories: [], currentChatHistory: [], getRequestHeaders: () => ({})});
        instance.initializeIdentity();
    }
    await app.logout();
    assert.equal(storage.has('chatHistories:user:1'), false);
    await assert.rejects(stale.authFetch('/api/chat'), /登录状态/);
    assert.equal(stale.identityInvalidated, true);
    assert.equal(requests, 1);
});

test('identity broadcast immediately empties stale UI and aborts pending request', async () => {
    const storage = new Map();
    let requestSignal;
    let release;
    const app = loadApp([], storage, async (url, options) => {
        requestSignal = options.signal;
        await new Promise(resolve => {release = resolve;});
        return {ok: true};
    });
    Object.assign(app, {currentUser: {id: 1}, chatMessages: {innerHTML: 'private A'},
        chatHistoryList: {innerHTML: 'old list'}, messageInput: {}, sendButton: {},
        currentChatHistory: [{content: 'private A'}], chatHistories: []});
    app.initializeIdentity();
    const pending = app.authFetch('/api/chat');
    const state = JSON.stringify({userId: 2, version: 'new-login'});
    storage.set('aiOnCallAuthState', state);
    app.testListeners.storage({key: 'aiOnCallAuthState', newValue: state});
    assert.equal(app.chatMessages.innerHTML, '');
    assert.equal(app.chatHistoryList.innerHTML, '');
    assert.equal(app.sendButton.disabled, true);
    assert.equal(requestSignal.aborted, true);
    release();
    await assert.rejects(pending, /登录状态/);
});

test('successful login publishes a new identity version before navigation', async () => {
    const storage = new Map([['aiOnCallAuthState', '{"userId":1,"version":"old"}']]);
    const elements = {};
    for (const id of ['loginForm', 'username', 'password', 'remember', 'loginError', 'loginButton', 'togglePassword']) {
        elements[id] = {value: 'password', addEventListener(name, callback) {this[name] = callback;},
            querySelector() {return {};}};
    }
    let stateAtNavigation;
    const context = {console, document: {getElementById: id => elements[id]},
        sessionStorage: {removeItem() {}},
        localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        window: {location: {replace() {stateAtNavigation = storage.get('aiOnCallAuthState');}}},
        fetch: async url => url.endsWith('/login')
            ? {ok: true, json: async () => ({user: {id: 2}})} : {ok: false}};
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/login.js'), 'utf8'), context);
    await elements.loginForm.submit({preventDefault() {}});
    assert.equal(JSON.parse(stateAtNavigation).userId, 2);
    assert.notEqual(JSON.parse(stateAtNavigation).version, 'old');
});

test('identity change during initial authentication cannot start an old account app', async () => {
    const storage = new Map([['aiOnCallAuthState', '{"userId":1,"version":"old"}']]);
    let release;
    const app = loadApp([], storage, async () => ({ok: true,
        json: () => new Promise(resolve => {release = resolve;})}));
    vm.runInContext('AIOnCallApp = class {constructor() {globalThis.started = true;}};', app.testContext);
    const initializing = app.testListeners['document:DOMContentLoaded']();
    while (!release) await Promise.resolve();
    storage.set('aiOnCallAuthState', '{"userId":2,"version":"new"}');
    release({id: 1});
    await initializing;
    assert.equal(app.testContext.started, undefined);
});

test('identity change cancels a live stream and rejects its pending chunk', async () => {
    const storage = new Map();
    let finishRead;
    let cancelled = false;
    const app = loadApp([], storage, async () => ({ok: true, body: {getReader: () => ({
        read: () => new Promise(resolve => {finishRead = resolve;}),
        cancel() {cancelled = true; finishRead({done: true});}, releaseLock() {},
    })}}));
    app.currentUser = {id: 1};
    app.initializeIdentity();
    const response = await app.authFetch('/api/chat_stream');
    const reader = response.body.getReader();
    const chunk = reader.read();
    const state = '{"userId":2,"version":"new"}';
    storage.set('aiOnCallAuthState', state);
    app.testListeners.storage({key: 'aiOnCallAuthState', newValue: state});
    await assert.rejects(chunk, /登录状态/);
    assert.equal(cancelled, true);
    reader.releaseLock();
});

test('identity change aborts a response whose JSON body is still downloading', async () => {
    const storage = new Map();
    let signal;
    let release;
    const app = loadApp([], storage, async (url, options) => {
        signal = options.signal;
        return {ok: true, body: {}, json: () => new Promise(resolve => {release = resolve;})};
    });
    app.currentUser = {id: 1};
    app.initializeIdentity();
    const response = await app.authFetch('/api/chat/session/old');
    const body = response.json();
    const state = '{"userId":2,"version":"new"}';
    storage.set('aiOnCallAuthState', state);
    app.testListeners.storage({key: 'aiOnCallAuthState', newValue: state});
    const wasAborted = signal.aborted;
    release({history: []});
    await assert.rejects(body, /登录状态/);
    assert.equal(wasAborted, true);
    assert.equal(app.requestControllers.size, 0);
});

test('expired authentication never falls back to private local history', async () => {
    const app = loadApp([], new Map(), async () => ({ok: false, status: 401}));
    Object.assign(app, {chatHistories: [{id: 'old', messages: [{type: 'user', content: 'private A'}]}],
        currentChatHistory: [], chatMessages: element(), rendered: [], getRequestHeaders: () => ({}),
        addMessage(type, content) {this.rendered.push(content);},
        checkAndSetCentered() {}, renderChatHistory() {}});
    await app.loadChatHistory('old');
    assert.equal(app.rendered.length, 0);
    assert.equal(app.currentChatHistory.length, 0);
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
        return {ok, json: async () => ({})};
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
