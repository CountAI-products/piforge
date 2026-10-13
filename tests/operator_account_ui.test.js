const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('index.html', 'utf8');
const start = source.indexOf('  function showOperatorUser(){');
const end = source.indexOf('  async function operatorFetch(', start);
assert(start >= 0 && end > start);
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {open:false, hidden:false, textContent:'', value:'', showModal(){this.open=true}, close(){this.open=false}});
  return nodes.get(id);
}
const main = {style:{}};
const context = vm.createContext({$:element, document:{querySelector:()=>main}, operatorUser:{username:'alice'}});
vm.runInContext(source.slice(start,end),context);
element('operator-dialog').open = true;
vm.runInContext('showOperatorUser()', context);
assert.equal(element('operator-dialog').open,true, 'Account popup must remain open after refresh');
assert.equal(element('operator-signed-in').hidden,false);
assert.equal(element('operator-form').hidden,true);
assert.equal(element('operator-logout').hidden,false);
context.operatorUser=null;
vm.runInContext('showOperatorUser()',context);
assert.equal(element('operator-dialog').open,true,'Logout must present login');
assert.equal(element('operator-form').hidden,false);
assert.equal(element('operator-signed-in').hidden,true);
assert.equal(main.style.visibility,'hidden');
console.log('Account popup remains open; logout shows locked login screen.');
