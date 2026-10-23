// The official package logs its startup message with console.log. MCP stdio
// reserves stdout for JSON-RPC; diagnostic output belongs exclusively on stderr.
'use strict';
const { format } = require('node:util');
console.log = (...args) => process.stderr.write(format(...args) + '\n');
console.info = console.log;
console.debug = console.log;
