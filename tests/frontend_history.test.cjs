const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function element() {
    return { style: {}, children: [], appendChild(child) { this.children.push(child); } };
}

function loadApp(history) {
    const context = {
        console,
        document: { createElement: element, head: element(), addEventListener() {} },
        fetch: async () => ({ ok: true, json: async () => ({ history }) }),
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8') +
        '\nglobalThis.App = AIOnCallApp;', context);
    return Object.create(context.App.prototype);
}

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
