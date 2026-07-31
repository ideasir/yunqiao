const state = { connected: false, pairCode: '------', ws: null, sessions: [], currentSessionId: null, deviceId: null, deviceName: '', cwd: '', logs: {}, platform: '', hostname: '', workDir: '', relayKey: '', permission: 'super' };
var isDesktopApp = typeof window.pywebview !== 'undefined';

function addLog(type, text) {
  const log = document.getElementById('sessionLog');
  const entry = document.createElement('div');
  entry.className = 'entry ' + type + ' flex gap-3 py-1 animate-slide-up';

  const now = new Date();
  const time = now.toTimeString().slice(0, 8);
  const colors = {cmd:'text-white font-medium',out:'text-[#e8e8e8]',err:'text-[#ff6b6b]',info:'text-[#aaaaaa]',done:'text-white font-medium',file:'text-[#cccccc]',code:'text-[#cccccc]',status:'text-[#cccccc]'};
  const c = colors[type] || 'text-[#cccccc]';
  if (type === 'file' || type === 'code') {
    entry.innerHTML = '<span class="text-[#777] flex-shrink-0 min-w-[60px]">' + time + '</span><span class="' + c + '"><pre style="margin:0;font-family:inherit;white-space:pre-wrap">' + esc(text) + '</pre></span>';
  } else {
    entry.innerHTML = '<span class="text-[#777] flex-shrink-0 min-w-[60px]">' + time + '</span><span class="' + c + '">' + esc(text) + '</span>';
  }
  log.appendChild(entry);
  log.scrollTop = log.scrollHeight;
}
function esc(t) { var d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

function sendCommand() {
  var input = document.getElementById('cmdInput');
  var text = input.value.trim();
  if (!text) return;
  input.value = '';
  addLog('cmd', '$ ' + text);
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: 'execute_command', requestId: 'cmd_' + Date.now(), payload: { command: text, timeout: 30000 } }));
    state.ws.send(JSON.stringify({ type: 'agent_message', requestId: 'msg_' + Date.now(), text: text }));
  } else {
    try { pywebview.api.api_send_command(text); } catch(e) { addLog('err', '未连接到服务器'); }
  }
}

function copyPairCode() {
  navigator.clipboard.writeText('云桥 配对码 ' + state.pairCode);
  addLog('info', '配对码已复制');
}
function refreshPairCode() {
  state.pairCode = String(100000 + Math.floor(Math.random() * 900000));
  var el = document.getElementById('pairCode'); if (el) el.textContent = state.pairCode;
  addLog('info', '配对码已刷新: ' + state.pairCode);
}

function openSessionModal() {
  document.getElementById('modalSessionName').value = '工作区 ' + (state.sessions.length + 1);
  document.getElementById('modalSessionDir').value = state.workDir || 'C:\\Users\\Administrator';
  var overlay = document.getElementById('sessionModal');
  var content = document.getElementById('sessionModalContent');
  overlay.style.opacity = '1';
  overlay.style.pointerEvents = 'auto';
  setTimeout(function() {
    content.style.transform = 'scale(1) translateY(0)';
    content.style.opacity = '1';
  }, 10);
  setTimeout(function() {
    document.getElementById('modalSessionName').focus();
    document.getElementById('modalSessionName').select();
  }, 100);
}
function closeSessionModal() {
  var overlay = document.getElementById('sessionModal');
  var content = document.getElementById('sessionModalContent');
  content.style.transform = 'scale(0.9) translateY(20px)';
  content.style.opacity = '0';
  setTimeout(function() {
    overlay.style.opacity = '0';
    overlay.style.pointerEvents = 'none';
  }, 200);
}
function createSession() {
  openSessionModal();
}

function openSessionModal() {
  document.getElementById('modalSessionName').value = '工作区 ' + (state.sessions.length + 1);
  document.getElementById('modalSessionDir').value = state.workDir || 'C:\\Users\\Administrator';
  var overlay = document.getElementById('sessionModal');
  var content = document.getElementById('sessionModalContent');
  overlay.style.opacity = '1';
  overlay.style.pointerEvents = 'auto';
  setTimeout(function() {
    content.style.transform = 'scale(1) translateY(0)';
    content.style.opacity = '1';
  }, 10);
  setTimeout(function() {
    document.getElementById('modalSessionName').focus();
    document.getElementById('modalSessionName').select();
  }, 100);
}
function saveSessionModal() {
  var name = document.getElementById('modalSessionName').value.trim() || '工作区';
  var dir = document.getElementById('modalSessionDir').value.trim() || 'C:\\Users\\Administrator';
  var id = 's_' + Date.now().toString(36);
  state.sessions.push({ id: id, name: name, workDir: dir, cwd: dir, isDefault: state.sessions.length === 0 });
  state.workDir = dir;
  // 如果没有默认会话，则设为默认
  if (!state.currentSessionId) {
    state.currentSessionId = id;
  }
  renderSidebar(); renderSessions(); updateStatus();
  addLog('info', '会话已创建: ' + name + ' (' + id + ')');
  var overlay = document.getElementById('sessionModal');
  var content = document.getElementById('sessionModalContent');
  content.style.transform = 'scale(0.9) translateY(20px)';
  content.style.opacity = '0';
  setTimeout(function() {
    overlay.style.opacity = '0';
    overlay.style.pointerEvents = 'none';
  }, 200);
}
function browseSessionDir(e) {
  var input = document.getElementById('modalSessionDir');
  var btn = e ? e.target : null;
  if (btn) { btn.textContent = '...'; btn.style.opacity = '0.5'; }
  
  // 尝试原生文件夹选择器
  try {

      window.showDirectoryPicker().then(function(handle) {
        document.getElementById('modalSessionDir').value = handle.name;
        if (btn) { btn.textContent = '浏览'; btn.style.opacity = '1'; }
      }).catch(function() {
        if (btn) { btn.textContent = '浏览'; btn.style.opacity = '1'; }
        input.focus();
        input.select();
      });
      return;
    }
  } catch(e) {}
  
  // 降级：手动输入
  if (btn) { btn.textContent = '浏览'; btn.style.opacity = '1'; }
  input.focus();
  input.select();
function refreshSessions() { addLog('info', '会话列表已刷新'); }
var ctxSessionId = null;
function renderSidebar() {
  var list = document.getElementById('sidebarList');
  if (!list) return;
  list.innerHTML = '';
  // 默认会话置顶
  var sorted = [].concat(state.sessions);
  sorted.sort(function(a, b) {
    if (a.id === state.currentSessionId) return -1;
    if (b.id === state.currentSessionId) return 1;
    return 0;
  });
  sorted.forEach(function(s) {
    var isDefault = s.id === state.currentSessionId;
    var item = document.createElement('div');
    item.className = 'flex items-center justify-center rounded-lg cursor-pointer text-xs font-semibold transition-all relative';
    if (isDefault) {
      item.style.cssText = 'background:#222;color:#4ade80;width:26px;height:26px;border:1px solid #4ade80;';
    } else {
      item.style.cssText = 'color:#666;width:26px;height:26px;background:transparent;';
    }
    item.title = (s.name || 'unnamed') + ' - ' + (s.workDir || '');
    item.textContent = (s.name || '?').slice(0, 1);
    item.onmouseover = function() { if (!isDefault) { this.style.background = '#1a1a1a'; this.style.color = '#aaa'; } };
    item.onmouseout = function() { if (!isDefault) { this.style.background = 'transparent'; this.style.color = '#666'; } };
    item.onclick = function(e) {
      e.stopPropagation();
      ctxSessionId = s.id;
      var menu = document.getElementById('ctxMenu');
      menu.style.display = 'block';
      menu.style.left = '48px';
      menu.style.top = Math.min(e.clientY, window.innerHeight - 80) + 'px';
    };
    list.appendChild(item);
  });
}
// 点击其他地方关闭菜单
document.addEventListener('click', function() {
  var menu = document.getElementById('ctxMenu');
  if (menu) menu.style.display = 'none';
});
function ctxSetDefault() {
  var menu = document.getElementById('ctxMenu');
  menu.style.display = 'none';
  if (ctxSessionId) setDefaultSession(ctxSessionId);
}
function ctxDelete() {
  var menu = document.getElementById('ctxMenu');
  menu.style.display = 'none';
  if (ctxSessionId) closeSession(ctxSessionId);
}

function renderSessions() {}

function switchSession(id) {
  // 查看会话日志（不切换默认工作区）
  var s = state.sessions.find(function(s) { return s.id === id; });
  if (s) {
    addLog('info', '查看会话: ' + (s.name || 'unnamed') + ' (' + id + ')');
  }
}

function setDefaultSession(id) {
  state.currentSessionId = id;
  renderSidebar();
  renderSessions();
  updateStatus();
  var s = state.sessions.find(function(s) { return s.id === id; });
  addLog('status', '默认工作区已切换: ' + (s ? s.name : id));
}
function closeSession(id) {
  if (!confirm('确定关闭此会话?')) return;
  state.sessions = state.sessions.filter(function(s) { return s.id !== id; });
  if (state.currentSessionId === id) { state.currentSessionId = state.sessions.length > 0 ? state.sessions[0].id : null; }
  renderSidebar(); renderSessions(); addLog('info', '会话已关闭: ' + id);
}

function setPermission(type) {
  state.permission = type;
  var superEl = document.getElementById('permSuper');
  var workspaceEl = document.getElementById('permWorkspace');
  if (type === 'super') {
    superEl.style.cssText = 'background: #222; border: 1px solid #333;';
    superEl.querySelector('div:first-child').className = 'w-1.5 h-1.5 rounded-full bg-[#ededed]';
    superEl.querySelector('div:last-child').className = 'text-[11px] font-medium text-[#ededed]';
    
    workspaceEl.style.cssText = 'background: #1a1a1a; border: 1px solid #222;';
    workspaceEl.querySelector('div:first-child').className = 'w-1.5 h-1.5 rounded-full bg-[#444]';
    workspaceEl.querySelector('div:last-child').className = 'text-[11px] font-medium text-[#666]';
    addLog('info', '权限模式: 超级');
  } else {
    workspaceEl.style.cssText = 'background: #222; border: 1px solid #333;';
    workspaceEl.querySelector('div:first-child').className = 'w-1.5 h-1.5 rounded-full bg-[#ededed]';
    workspaceEl.querySelector('div:last-child').className = 'text-[11px] font-medium text-[#ededed]';
    
    superEl.style.cssText = 'background: #1a1a1a; border: 1px solid #222;';
    superEl.querySelector('div:first-child').className = 'w-1.5 h-1.5 rounded-full bg-[#444]';
    superEl.querySelector('div:last-child').className = 'text-[11px] font-medium text-[#666]';
    addLog('info', '权限模式: 工作区');
  }
}

function updateStatus() {
  var el;
  el = document.getElementById('pairCode'); if (el) el.textContent = state.pairCode;
  el = document.getElementById('deviceName'); if (el) el.textContent = state.deviceName || '-';
  el = document.getElementById('client平台'); if (el) el.textContent = state.platform || '-';
  el = document.getElementById('client主机名'); if (el) el.textContent = state.hostname || '-';
  el = document.getElementById('workDir'); if (el) el.textContent = state.workDir || '未设置';
  el = document.getElementById('currentCwd'); if (el) el.textContent = state.workDir || '~';

}
function handleBridge(action, data) {
  if (action === 'log') { addLog('info', data.text); }
  else if (action === 'command_result') {
    var p = data.payload;
    if (p && p.exitCode !== undefined) {
      var txt = p.stdout || p.stderr || '(无输出)';
      addLog('out', txt);
      if (p.exitCode !== 0) addLog('err', '退出码: ' + p.exitCode);
      else addLog('done', '完成 (退出码: ' + p.exitCode + ')');
    }
  }
  else if (action === 'agent_status') {
    var s = document.getElementById('agent状态');
    if (data.status === 'connected') { s.textContent = '已配对'; s.style.color = '#4ade80'; }
    else { s.textContent = '待接入'; s.style.color = '#bbb'; }
  }
  else if (action === 'relay_status') {
    var dot = document.getElementById('relayDot');
    if (data.status === 'connected') { dot.className = 'w-1.5 h-1.5 rounded-full bg-[#4ade80] animate-pulse'; }
    else { dot.className = 'w-1.5 h-1.5 rounded-full bg-[#444]'; }
  }
}
function setConn状态(status) {
  var dot = document.getElementById('conn状态');
  dot.className = 'w-2 h-2 rounded-full ' + (status === 'connected' ? 'bg-[#ededed]' : 'bg-[#444]');
}

(function() {
  refreshPairCode();
  updateStatus();
  addLog('info', '欢迎使用云桥 MCP v2.0');
  setConn状态('disconnected');
})();



function connectRelay() {
  var url = document.getElementById('relayUrl').textContent;
  addLog('status', '正在连接中转服务器: ' + url);
  if (state.ws) {
    try { state.ws.close(); } catch(e) {}
  }
  state.ws = new WebSocket('wss://' + url + '/device');
  state.ws.onopen = function() {
    addLog('done', '中转服务器连接成功');
    document.getElementById('relayStatus').textContent = '已连接';
    document.getElementById('relayDot').className = 'w-1.5 h-1.5 rounded-full bg-[#4ade80] animate-pulse';
    var clientDots = document.querySelectorAll('.rounded-xl.bg-\[\#141414\] .w-1\.5\.h-1\.5\.rounded-full');
    if (clientDots.length > 0) {
      clientDots[clientDots.length - 1].className = 'w-1.5 h-1.5 rounded-full bg-[#4ade80]';
    }
    state.ws.send(JSON.stringify({
      type: 'register', deviceName: 'web-ui', os: 'web', arch: 'web',
      hostname: location.hostname, authCode: state.pairCode
    }));
  };
  state.ws.onmessage = function(e) {
    try {
      var msg = JSON.parse(e.data);
      if (msg.type === 'agent_connected') {
        addLog('status', 'Agent 已配对');
        document.getElementById('agent状态').textContent = '已配对';
        document.getElementById('agent状态').style.color = '#4ade80';
        var dots = document.querySelectorAll('.w-1\.5\.h-1\.5\.rounded-full');
        if (dots.length > 0) dots[0].className = 'w-1.5 h-1.5 rounded-full bg-[#4ade80] animate-pulse';
      } else if (msg.type === 'agent_disconnected') {
        document.getElementById('agent状态').textContent = '已断开';
        document.getElementById('agent状态').style.color = '#bbb';
        var dots = document.querySelectorAll('.w-1\.5\.h-1\.5\.rounded-full');
        if (dots.length > 0) dots[0].className = 'w-1.5 h-1.5 rounded-full bg-[#666]';
      } else if (msg.type === 'command_result' || msg.type === 'session_op_result') {
        trafficFlash();
      }
    } catch(e) {}
  };
  state.ws.onclose = function() {
    addLog('info', '中转服务器连接断开');
    document.getElementById('relayStatus').textContent = '未连接';
    document.getElementById('relayDot').className = 'w-1.5 h-1.5 rounded-full bg-[#444]';
    var clientDots = document.querySelectorAll('.rounded-xl.bg-\[\#141414\] .w-1\.5\.h-1\.5\.rounded-full');
    if (clientDots.length > 0) {
      clientDots[clientDots.length - 1].className = 'w-1.5 h-1.5 rounded-full bg-[#444]';
    }
  };
  state.ws.onerror = function() {
    addLog('err', '中转服务器连接失败');
  };
}
var trafficTimer = null;
function trafficFlash() {
  var relayDot = document.getElementById('relayDot');
  if (trafficTimer) clearTimeout(trafficTimer);
  relayDot.className = 'w-1.5 h-1.5 rounded-full bg-[#22d3ee]';
  trafficTimer = setTimeout(function() {
    relayDot.className = 'w-1.5 h-1.5 rounded-full bg-[#4ade80] animate-pulse';
  }, 500);
}
function configRelay() {
  document.getElementById('modalRelayUrl').value = document.getElementById('relayUrl').textContent;
  document.getElementById('modalRelayKey').value = state.relayKey || '';
  var overlay = document.getElementById('configModal');
  var content = document.getElementById('configModalContent');
  overlay.style.opacity = '1';
  overlay.style.pointerEvents = 'auto';
  setTimeout(function() {
    content.style.transform = 'scale(1) translateY(0)';
    content.style.opacity = '1';
  }, 10);
  setTimeout(function() {
    document.getElementById('modalRelayUrl').focus();
    document.getElementById('modalRelayUrl').select();
  }, 100);
}
function closeConfigModal() {
  var overlay = document.getElementById('configModal');
  var content = document.getElementById('configModalContent');
  content.style.transform = 'scale(0.9) translateY(20px)';
  content.style.opacity = '0';
  setTimeout(function() {
    overlay.style.opacity = '0';
    overlay.style.pointerEvents = 'none';
  }, 200);
}
function saveConfigModal() {
  var url = document.getElementById('modalRelayUrl').value.trim();
  var key = document.getElementById('modalRelayKey').value.trim();
  if (url) document.getElementById('relayUrl').textContent = url;
  if (key) state.relayKey = key;
  addLog('info', '中转服务器配置已更新');
  closeConfigModal();
}