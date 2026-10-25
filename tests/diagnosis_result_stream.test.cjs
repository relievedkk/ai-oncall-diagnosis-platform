const test = require('node:test');
const assert = require('node:assert/strict');
const {DiagnosisClient, progressText} = require('../static/diagnosis-stream.js');

test('terminal reconstruction preserves result status through refresh without confirming a cause', async () => {
    const values = new Map();
    const storage = {getItem: k => values.get(k), setItem: (k,v) => values.set(k,v), removeItem: k => values.delete(k)};
    const result = {schema_version: 1, outcome: 'insufficient_evidence', summary: 'logs missing'};
    const client = new DiagnosisClient({userId: 'a', storage, fetch: async (_url, options) => {
        if (options.method === 'POST') return Response.json({diagnosis_id:'d',events_url:'/events'});
        return new Response(`id: terminal:1:completed\ndata: ${JSON.stringify({type:'complete',response:'partial',diagnosis_result:result})}\n\n`);
    }});
    await client.start({});
    const restored = new DiagnosisClient({userId:'a', storage, fetch: async () => {throw Error('should not fetch');}});
    assert.deepEqual(restored.state.progress.diagnosisResult, result);
    assert.match(progressText(restored.state), /证据不足/);
    assert.doesNotMatch(progressText(restored.state), /根因已确认/);
});

test('legacy report and exhausted budget have distinct visible labels', () => {
    assert.match(progressText({progress:{report:'old'}}), /未进行结构化校验/);
    assert.match(progressText({progress:{report:'partial',diagnosisResult:{outcome:'budget_exhausted'}}}), /预算耗尽/);
    assert.match(progressText({progress:{report:'new',diagnosisResult:{outcome:'diagnosed'}}}), /待人工复核/);
});
