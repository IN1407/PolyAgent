// Exercise frontend rendering with a small DOM fixture. This is not a harness integration test.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = new Map();
function element(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {
    innerHTML: '', value: '', hidden: false,
    insertAdjacentHTML(_position, html) { this.innerHTML += html; },
    classList: { toggle() {} },
  });
  return nodes.get(selector);
}
const context = vm.createContext({
  document: {
    querySelector: selector => selector.startsWith('[data-chat-mode') ? null : element(selector),
    addEventListener() {},
  },
  localStorage: { getItem() { return null; } },
  URL,
});
const source = fs.readFileSync('static/app.js', 'utf8').split("$('#project-form').addEventListener")[0];
vm.runInContext(source, context);
vm.runInContext(`
state={projects:[{id:'p',name:'Project',mode:'manual',orchestrator_model:'m'}],
  models:[{id:'m',harness:'deepseek_harness',display_name:'Model'}],
  harness_modes:{deepseek_harness:{fields:[{key:'approval',label:'Permission preset'},
  {key:'execution',label:'Agent preset',creation_only:true}],approval:[{value:'harness',label:'Use harness settings'},{value:'read-only',label:'Read only'}],
  execution:[{value:'harness',label:'Use harness settings'},{value:'ptc',label:'PTC'}],last_used:{execution:'ptc'}}}};
selectedProject='p';selectedConversation='a';
conversations=[{id:'a',title:'First chat',settings:{}},{id:'b',title:'Second chat',settings:{}}];
chats=[{id:'native-a',conversation_id:'a',role:'orchestrator',model:'m',harness:'deepseek_harness'},
       {id:'native-b',conversation_id:'b',role:'orchestrator',model:'m',harness:'deepseek_harness'}];
events=[{id:1,chat_id:'native-a',conversation_id:'a',kind:'user',text:'FIRST_CONTEXT'},
        {id:2,chat_id:'native-b',conversation_id:'b',kind:'user',text:'SECOND_CONTEXT'},
        {id:3,chat_id:'child-a',conversation_id:'a',kind:'subagent_response',text:'CHILD_FOR_FIRST',metadata:{}}];
renderConversations();renderEvents();
`, context);
assert.match(element('#conversation-select').innerHTML, /First chat/);
assert.match(element('#conversation-select').innerHTML, /Second chat/);
assert.match(element('#harness-mode-controls').innerHTML, /value="ptc"[^>]* selected/);
assert.match(element('#harness-mode-controls').innerHTML, /Agent preset/);
assert.match(element('#harness-mode-controls').innerHTML, /data-chat-mode="execution"[^>]*disabled/);
assert.doesNotMatch(element('#harness-mode-controls').innerHTML, /Action approval|Execution mode|Sandbox/);
assert.match(element('#chat-events').innerHTML, /FIRST_CONTEXT/);
assert.match(element('#chat-events').innerHTML, /CHILD_FOR_FIRST/);
assert.doesNotMatch(element('#chat-events').innerHTML, /SECOND_CONTEXT/);
vm.runInContext("selectedConversation='b';renderConversations();renderEvents();", context);
assert.match(element('#chat-events').innerHTML, /SECOND_CONTEXT/);
assert.doesNotMatch(element('#chat-events').innerHTML, /FIRST_CONTEXT|CHILD_FOR_FIRST/);
vm.runInContext(`
state.models.push({id:'codex-model',harness:'codex'});
state.projects[0].orchestrator_model='codex-model';
state.harness_modes.codex={fields:[{key:'approval',label:'Approval policy'},{key:'sandbox',label:'Sandbox mode'}],
  approval:[{value:'harness',label:'Use harness settings'},{value:'untrusted',label:'Ask for untrusted actions'}],
  sandbox:[{value:'harness',label:'Use harness settings'},{value:'read-only',label:'Read only'}]};
renderConversations();
`, context);
assert.match(element('#harness-mode-controls').innerHTML, /Approval policy/);
assert.match(element('#harness-mode-controls').innerHTML, /Sandbox mode/);
assert.doesNotMatch(element('#harness-mode-controls').innerHTML, /Agent preset|PTC|Permission preset/);
vm.runInContext(`
selectedConversation='a';
chats.push({id:'child',conversation_id:'a',role:'subagent',model:'m',status:'completed'});
events=[{id:10,chat_id:'child',conversation_id:'a',kind:'assistant',text:'**Done** <img src=x onerror=alert(1)> [file](file:///tmp/project/index.html)',metadata:{model:'m'}},
        {id:11,chat_id:'native-a',conversation_id:'a',kind:'subagent_response',text:'**Done** <img src=x onerror=alert(1)> [file](file:///tmp/project/index.html)',metadata:{subagent_chat_id:'child'}},
        {id:12,conversation_id:'a',kind:'tool',text:'CORTEX_STEP_TYPE_RUN_COMMAND',metadata:{phase:'CORTEX_STEP_STATUS_DONE',detail:{command:'pwd',exit_code:0}}}];
sendingTarget={project:'p',conversation:'a',auto:true};renderEvents();
`, context);
const chat=element('#chat-events').innerHTML;
assert.match(chat, /<strong>Done<\/strong>/);
assert.match(chat, /&lt;img/);
assert.doesNotMatch(chat, /<img/);
assert.equal((chat.match(/<strong>Done<\/strong>/g)||[]).length,1);
assert.match(chat, /data-open-file="\/tmp\/project\/index.html"/);
assert.match(chat, /Terminal command.*pwd/);
assert.match(chat, /Choosing an orchestrator model/);
console.log('Frontend chat isolation, native modes, safe formatting, deduplication, progress, and file links passed.');
