/* Durable diagnosis subscriptions. No framework or external dependencies. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.DiagnosisStream = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    class SSEParser {
        constructor(onEvent) {
            this.onEvent = onEvent;
            this.decoder = new TextDecoder();
            this.buffer = '';
            this.resetFrame();
        }
        resetFrame() { this.data = []; this.id = ''; this.event = 'message'; }
        push(bytes) {
            this.buffer += this.decoder.decode(bytes, {stream: true});
            while (true) {
                const index = this.buffer.search(/[\r\n]/);
                if (index < 0 || (this.buffer[index] === '\r' && index === this.buffer.length - 1)) return;
                const length = this.buffer[index] === '\r' && this.buffer[index + 1] === '\n' ? 2 : 1;
                const line = this.buffer.slice(0, index);
                this.buffer = this.buffer.slice(index + length);
                this.line(line);
            }
        }
        line(line) {
            if (line === '') {
                if (this.data.length) this.onEvent({id: this.id, event: this.event, data: this.data.join('\n')});
                this.resetFrame();
                return;
            }
            if (line[0] === ':') return;
            const colon = line.indexOf(':');
            const field = colon < 0 ? line : line.slice(0, colon);
            let value = colon < 0 ? '' : line.slice(colon + 1);
            if (value[0] === ' ') value = value.slice(1);
            if (field === 'data') this.data.push(value);
            else if (field === 'event') this.event = value;
            else if (field === 'id' && !value.includes('\0')) this.id = value;
        }
        end() {
            // EOF is not a frame boundary. Incomplete UTF-8, fields and data are
            // discarded; the next connection replays from the last applied ID.
            if (this.buffer.endsWith('\r')) this.line(this.buffer.slice(0, -1));
            this.buffer = '';
            this.resetFrame();
        }
    }

    const abortError = () => Object.assign(new Error('Subscription disconnected'), {name: 'AbortError'});
    function wait(ms, signal) {
        return new Promise((resolve, reject) => {
            if (signal.aborted) { reject(abortError()); return; }
            const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
            function abort() { clearTimeout(timer); reject(abortError()); }
            signal.addEventListener('abort', abort, {once: true});
        });
    }
    function reduceProgress(progress, event) {
        const next = {log: progress?.log || '', report: progress?.report || '', diagnosisResult: progress?.diagnosisResult || null};
        if (event.diagnosis_result && typeof event.diagnosis_result === 'object') next.diagnosisResult = event.diagnosis_result;
        if (event.type === 'report') {
            next.report = event.report || next.report;
            const skipped = Array.isArray(event.skipped_steps) ? event.skipped_steps.length : 0;
            if (skipped) next.log += `\n⏭️ 当前场景无需执行的 ${skipped} 个步骤已跳过。\n`;
        }
        else if (event.type === 'complete') next.report = event.response || event.diagnosis?.report || next.report;
        else if (event.type === 'content') next.log += event.data || '';
        else if (event.type === 'plan') next.log += `\n\n## 📋 执行计划\n${event.message || ''}\n${(event.plan || []).map(x => `- ${x}`).join('\n')}\n`;
        else if (event.type === 'step_complete') next.log += `\n✅ ${event.message || event.current_step || '步骤完成'}\n`;
        else if (event.type === 'evidence') next.log += `\n🔎 ${event.summary || event.message || '已采集诊断证据'}\n`;
        else if (event.type === 'status') next.log += `\n⏳ ${event.message || ''}\n`;
        else if (event.type === 'error') {
            next.log += `\n${event.message || event.data || '诊断任务未完成'}\n`;
            next.report = event.response || next.report;
        }
        return next;
    }
    function progressText(state) {
        const progress = state?.progress || {};
        const labels = {diagnosed: '诊断结论（待人工复核）', insufficient_evidence: '证据不足',
            budget_exhausted: '诊断预算耗尽', dependency_failed: '诊断依赖失败',
            no_active_alerts: '当前范围无活跃告警', legacy_unverified: '历史报告，未进行结构化校验'};
        const label = labels[progress.diagnosisResult?.outcome] || labels.legacy_unverified;
        return (progress.log || '') + (progress.report ? `\n\n## 🎯 诊断报告\n\n**${label}**\n\n${progress.report}` : '');
    }

    class DiagnosisClient {
        constructor(options) {
            this.userId = String(options.userId);
            this.fetch = options.fetch || globalThis.fetch.bind(globalThis);
            this.storage = options.storage;
            this.headers = options.headers || (() => ({}));
            this.isCurrent = options.isCurrent || (() => true);
            this.onUpdate = options.onUpdate || (() => {});
            this.onRetry = options.onRetry || (() => {});
            this.sleep = options.sleep || wait;
            this.keyFactory = options.keyFactory || (() => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`);
            this.baseUrl = options.baseUrl || '/api';
            this.state = this.load();
            this.controller = null;
            this.running = null;
        }
        get storageKey() { return `oncallDiagnosis:user:${this.userId}`; }
        load() {
            try {
                const state = JSON.parse(this.storage?.getItem(this.storageKey) || 'null');
                return state && String(state.userId) === this.userId ? state : null;
            } catch (_) { return null; }
        }
        save() {
            try { this.storage?.setItem(this.storageKey, JSON.stringify(this.state)); } catch (_) { /* Optional local cache. */ }
        }
        stop(clear = false) {
            this.controller?.abort();
            if (clear) {
                try { this.storage?.removeItem(this.storageKey); } catch (_) { /* Optional local cache. */ }
                this.state = null;
            }
        }
        setUser(userId) { this.stop(true); this.userId = String(userId); }
        start(payload) {
            if (this.running) return this.running;
            // Retain an unresolved creation across repeated clicks and refreshes.
            if (!this.state || this.state.done) {
                this.state = {userId: this.userId, idempotencyKey: this.keyFactory(), payload,
                    diagnosisId: null, eventsUrl: null, lastEventId: '', lastEvidenceId: '0',
                    progress: {log: '', report: ''}, done: false};
                this.save();
            }
            return this.resume();
        }
        resume() {
            if (this.running) return this.running;
            if (!this.state || this.state.done) return Promise.resolve(this.state);
            const controller = new AbortController();
            this.controller = controller;
            const actor = this.userId;
            const check = () => {
                if (controller.signal.aborted || this.userId !== actor || !this.isCurrent()) throw abortError();
            };
            this.running = this.run(controller.signal, check).finally(() => {
                if (this.controller === controller) { this.running = null; this.controller = null; }
            });
            return this.running;
        }
        async run(signal, check) {
            let failures = 0;
            while (true) {
                check();
                try {
                    if (!this.state.diagnosisId) {
                        const response = await this.fetch(`${this.baseUrl}/aiops/jobs`, {
                            method: 'POST', credentials: 'same-origin', signal,
                            headers: {...this.headers(), 'Content-Type': 'application/json', 'Idempotency-Key': this.state.idempotencyKey},
                            body: JSON.stringify(this.state.payload),
                        });
                        check();
                        this.checkResponse(response);
                        const job = await response.json();
                        check();
                        if (!job.diagnosis_id || !job.events_url) throw new Error('Invalid diagnosis creation response');
                        this.state.diagnosisId = job.diagnosis_id;
                        this.state.eventsUrl = job.events_url;
                        this.save();
                    }
                    const headers = {...this.headers(), Accept: 'text/event-stream'};
                    if (this.state.lastEventId) headers['Last-Event-ID'] = this.state.lastEventId;
                    const response = await this.fetch(this.state.eventsUrl, {method: 'GET', credentials: 'same-origin', headers, signal});
                    check();
                    this.checkResponse(response);
                    const reader = response.body.getReader();
                    const parser = new SSEParser(frame => {
                        check();
                        if (this.state.done) return;
                        const event = JSON.parse(frame.data);
                        const id = frame.id || event.event_id;
                        if (!id) throw new Error('Diagnosis event has no persistent ID');
                        if (id === this.state.lastEventId) return;
                        if (/^[0-9]+$/.test(id) && BigInt(id) <= BigInt(this.state.lastEvidenceId || '0')) return;
                        const next = {...this.state, progress: reduceProgress(this.state.progress, event), lastEventId: id};
                        if (/^[0-9]+$/.test(id)) next.lastEvidenceId = id;
                        next.done = event.type === 'complete' || event.type === 'error';
                        if (event.type === 'error') next.error = event.message || event.data || '诊断任务未完成';
                        this.onUpdate(next, event);
                        check();
                        this.state = next;
                        this.save();
                        failures = 0;
                    });
                    try {
                        while (!this.state.done) {
                            const chunk = await reader.read();
                            check();
                            if (chunk.done) break;
                            parser.push(chunk.value);
                        }
                    } finally {
                        parser.end();
                        try { await reader.cancel(); } catch (_) { /* Already disconnected. */ }
                        reader.releaseLock();
                    }
                    if (this.state.done) return this.state;
                    throw new Error('诊断连接中断，后台任务仍在运行');
                } catch (error) {
                    check();
                    if (error.permanent) throw error;
                    const delay = Math.min(30000, 1000 * 2 ** Math.min(failures++, 5));
                    this.onRetry(delay, error);
                    await this.sleep(delay, signal);
                }
            }
        }
        checkResponse(response) {
            if (response.ok) return;
            const error = new Error(`诊断请求失败: HTTP ${response.status}`);
            error.status = response.status;
            error.permanent = response.status < 500 && ![408, 429].includes(response.status);
            throw error;
        }
    }
    return {SSEParser, DiagnosisClient, progressText};
});
