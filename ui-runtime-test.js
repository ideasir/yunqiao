// 运行时验证：加载 ui.html 脚本（带 DOM stub + 假 pywebview），完整走一遍创建/切换/关闭工作区流程
// 目标：证明 "loadSessions is not defined" 不再出现
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(require('path').join(__dirname, 'client', 'ui.html'), 'utf8');
const scripts = [...html.matchAll(/<script>(.*?)<\/script>/gs)].map(m => m[1]);
const js = scripts.reduce((a, b) => (a.length > b.length ? a : b), '');
console.log('提取脚本长度:', js.length);

// ─── 通用 DOM stub（任何属性/方法都可链式调用） ───
function el() {
  const e = {
    children: [], style: {}, dataset: {}, className: '', title: '', value: '', id: '', draggable: false, _html: '', _tc: '',
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, remove() {}, focus() {}, select() {},
    setAttribute() {}, getAttribute() { return null; },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); },
    querySelector() { return el(); }, querySelectorAll() { return []; }, closest() { return null; },
    onclick: null, oncontextmenu: null, onmouseover: null, onmouseout: null,
  };
  // textContent 与 innerHTML 互相同步，保证 esc() 等逻辑可用
  Object.defineProperty(e, 'innerHTML', { get() { return this._html; }, set(v) { this._html = v; } });
  Object.defineProperty(e, 'textContent', {
    get() { return this._tc; },
    set(v) { this._tc = v; this._html = String(v); },
  });
  return e;
}

const sessionLog = el();  // 记录日志的容器
const inputs = { modalSessionName: '测试区', modalSessionDir: 'C:\\Work\\Test' };

// ─── 假 pywebview：create_session 成功、get_sessions 返回数据 ───
const apiCalls = [];
const fakeApi = {
  set_permission: async () => { apiCalls.push('set_permission'); return { success: true }; },
  get_status: async () => ({ pairCode: '123456', deviceName: 'PC', platform: 'Windows', hostname: 'pc', homeDir: 'C:\\Users\\Test' }),
  get_sessions: async () => ({ sessions: [{ id: 'default', name: '默认工作区', workDir: 'C:\\ws', cwd: 'C:\\ws' }], currentId: 'default', workDir: 'C:\\ws' }),
  create_session: async (dir, name) => { apiCalls.push('create_session'); return { success: true, session: { id: 's_new', name, workDir: dir } }; },
  switch_session: async () => { apiCalls.push('switch_session'); return { success: true }; },
  close_session: async () => { apiCalls.push('close_session'); return { success: true }; },
};

globalThis.window = { pywebview: { api: fakeApi }, confirm: () => true, showDirectoryPicker: undefined };
globalThis.document = {
  getElementById: id => (id === 'sessionLog' ? sessionLog : (id in inputs ? Object.assign(el(), { value: inputs[id] }) : el())),
  createElement: () => el(),
  addEventListener() {},
  body: el(),
  execCommand: () => false,
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.WebSocket = class { static OPEN = 1; };
globalThis.window.confirm = () => true;

const errors = [];
process.on('unhandledRejection', e => errors.push('unhandledRejection: ' + (e && e.message)));
process.on('uncaughtException', e => errors.push('uncaughtException: ' + e.message));

// 执行脚本（与浏览器同上下文）
vm.runInThisContext(js, { filename: 'ui.html' });

const sleep = ms => new Promise(r => setTimeout(r, ms));
const logText = () => sessionLog.children.map(c => c._html || '').join('|');
const hasErr = () => logText().includes('创建工作区失败') || errors.length > 0;

(async () => {
  let pass = 0, fail = 0;
  function check(name, cond, extra) {
    (cond ? pass++ : fail++);
    console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  (' + extra + ')' : ''}`);
  }

  // 1. 全局作用域验证（本次 bug 的根因）
  check('loadSessions 是全局函数', typeof globalThis.loadSessions === 'function');
  check('loadStatus 是全局函数', typeof globalThis.loadStatus === 'function');

  // 2. 等初始化定时器（loadStatus/loadSessions 300ms）跑完
  await sleep(500);
  // state 是 const 声明（全局词法环境），要通过同上下文执行取
  const getState = k => { try { return vm.runInThisContext('state.' + k); } catch { return undefined; } };
  check('初始化后 homeDir 已加载', getState('homeDir') === 'C:\\Users\\Test', getState('homeDir'));

  // 3. 完整走 saveSessionModal（点"确定"创建工作区）
  vm.runInThisContext('saveSessionModal()');
  await sleep(50);
  check('创建工作区成功（无 loadSessions 报错）', logText().includes('工作区已创建: 测试区'), logText());
  check('创建流程无任何失败日志', !hasErr(), errors.join(','));

  // 4. setDefaultSession（切换工作区）
  vm.runInThisContext("setDefaultSession('default')");
  await sleep(50);
  check('切换工作区成功', logText().includes('工作区已切换'), logText());

  // 5. closeSession（关闭工作区）
  vm.runInThisContext("closeSession('default')");
  await sleep(50);
  check('关闭工作区成功', logText().includes('会话已关闭'), logText());

  // 6. 全程无未处理异常
  check('全程无未处理异常', errors.length === 0, errors.join(','));

  console.log(`\n==== 结果: ${pass}/${pass + fail} 通过 ====`);
  process.exit(fail > 0 ? 1 : 0);
})();
