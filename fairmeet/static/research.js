/* Explicit, member-scoped research. Only the last run ID is persisted. */
(function () {
  'use strict';
  var panel = document.getElementById('research-panel');
  if (!panel) return;

  var trip = panel.dataset.trip;
  var member = panel.dataset.member;
  var base = '/api/trips/' + encodeURIComponent(trip);
  var storageKey = 'fairmeet.research.v1:' + encodeURIComponent(trip) + ':' + encodeURIComponent(member);
  var terminal = new Set(['succeeded', 'partial', 'failed', 'cancelled', 'interrupted']);
  var statuses = {queued: '等待检索', running: '正在检索', succeeded: '检索完成 · 等待采纳', partial: '部分完成 · 请留意资料缺口', failed: '检索失败', cancelled: '已取消检索', interrupted: '检索已中断'};
  var scopeLabels = {body: '正文', snippet: '仅摘要', link: '仅链接', structured: '地图地点记录（非点评/攻略）'};
  var state = {
    sources: [], source: 'fake', sourcesReady: false, loadingSources: false,
    consent: false, savingConsent: false, run: null, creating: false,
    restoring: false, cancelling: false, pollStopped: false, polling: false,
    timer: null, retry: null, uncertainCreate: false, lastCreate: null,
    candidateViews: new Map(), proposalViews: new Map(), pendingActions: 0,
    days: null, daysRequest: null, unknownRunId: null, readVersion: 0, leaving: false,
    authorizationRequired: false, planner: null
  };
  var ui = {};
  ['start', 'source-list', 'source-mode', 'consent', 'consent-wrap', 'settings-feedback',
    'reload-sources', 'intent', 'scope', 'results', 'run-mode', 'status', 'stage',
    'cancel', 'run-feedback', 'retry', 'stale', 'times', 'reply', 'errors', 'candidates',
    'documents', 'documents-summary', 'document-list', 'planner', 'plan-open', 'plan-fields',
    'plan-days', 'plan-candidates', 'model-consent', 'geo-consent', 'plan-submit', 'plan-feedback', 'plan-output'].forEach(function (name) {
    ui[name] = document.getElementById('research-' + name);
  });
  var messageInput = document.getElementById('ask-msg');
  var chatForm = document.getElementById('ask-form');
  var sendButton = chatForm.querySelector('button[type="submit"]');

  function text(value) { return value == null ? '' : String(value); }
  function list(value) { return Array.isArray(value) ? value : []; }
  function node(tag, className, value) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = text(value);
    return element;
  }
  function button(label, action) {
    var element = node('button', 'button-secondary', label);
    element.type = 'button';
    element.addEventListener('click', action);
    return element;
  }
  function uuid() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    var bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(bytes);
    else for (var i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    var hex = Array.from(bytes, function (value) { return value.toString(16).padStart(2, '0'); }).join('');
    return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20);
  }
  function safeURL(value) {
    if (typeof value !== 'string' || !/^https?:\/\//i.test(value)) return null;
    try {
      var url = new URL(value);
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
      return url.href;
    } catch (_) { return null; }
  }
  function sourceLink(value, label) {
    var url = safeURL(value);
    if (!url) return node('span', 'fm-research-help', '无可用公开链接');
    var link = node('a', '', label);
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.referrerPolicy = 'no-referrer';
    return link;
  }
  function dateLabel(value) {
    if (!value) return '未知';
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString('zh-CN', {hour12: false});
  }
  function remember(id) {
    try {
      if (id) window.localStorage.setItem(storageKey, text(id));
      else window.localStorage.removeItem(storageKey);
    } catch (_) { /* Storage may be unavailable; the current page still works. */ }
  }
  async function request(path, method, payload, timeoutMs) {
    var controller = new AbortController();
    var timeout = window.setTimeout(function () { controller.abort(); }, timeoutMs || 20000);
    var options = {method: method || 'GET', signal: controller.signal, credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}};
    if (payload !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(payload);
    }
    try {
      var response = await fetch(base + path + '?member_id=' + encodeURIComponent(member), options);
      var data;
      try { data = await response.json(); } catch (_) { data = null; }
      if (!response.ok) {
        var error = new Error(data && typeof data.detail === 'string' ? data.detail : '请求未完成（' + response.status + '），请稍后重试。');
        error.status = response.status;
        throw error;
      }
      if (data == null) throw new Error('服务未返回可读取的数据，请重试。');
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('请求超时，已停止自动读取。请手动重试。');
      if (error instanceof TypeError) throw new Error('网络连接中断，已停止自动读取。请检查连接后重试。');
      throw error;
    } finally { window.clearTimeout(timeout); }
  }
  function activeRun() { return state.run && !terminal.has(state.run.status); }
  function locked() { return state.creating || state.restoring || !!activeRun() || !!state.unknownRunId || state.uncertainCreate || state.pendingActions > 0; }
  function selectedSource() { return state.sources.find(function (source) { return source.key === state.source; }); }
  function isRealSource(source) { return source && source.key !== 'fake' && !source.is_simulated; }
  function canBuild(candidate) {
    return state.run && ['succeeded', 'partial'].includes(state.run.status) && !state.run.context_stale && !expiredRun() && candidate.match_status === 'simulated';
  }
  function expiredRun() {
    if (!state.run || !state.run.expires_at) return false;
    var expiry = new Date(state.run.expires_at).getTime();
    return !Number.isNaN(expiry) && expiry <= Date.now();
  }
  function refreshControls() {
    var source = selectedSource();
    var chatBusy = chatForm.getAttribute('aria-busy') === 'true';
    ui.start.disabled = state.authorizationRequired || locked() || chatBusy || !state.sourcesReady || state.loadingSources || state.savingConsent || !source || !source.available || (isRealSource(source) && !state.consent);
    ui.start.textContent = state.creating ? '正在创建检索…' : activeRun() ? '检索进行中…' : '先查资料';
    panel.querySelectorAll('input[name="research-source"]').forEach(function (input) {
      var current = state.sources.find(function (item) { return item.key === input.value; });
      input.disabled = state.authorizationRequired || locked() || state.loadingSources || !current || !current.available;
    });
    ui.consent.disabled = state.authorizationRequired || state.pendingActions > 0 || state.savingConsent || state.loadingSources || state.creating || state.restoring || state.uncertainCreate || !!state.unknownRunId;
    ui.intent.disabled = state.authorizationRequired || locked();
    ui.scope.disabled = state.authorizationRequired || locked();
    ui['reload-sources'].disabled = state.authorizationRequired || locked() || state.loadingSources;
    ui.cancel.hidden = !activeRun();
    ui.cancel.disabled = state.cancelling || state.creating || state.restoring;
    ui.cancel.textContent = state.cancelling ? '正在取消…' : '取消检索';
    // Keep ordinary chat independent, but do not reload the page in mid-research.
    sendButton.disabled = chatBusy || locked();
    ui.candidates.setAttribute('aria-busy', state.creating || (!!activeRun() && !state.pollStopped) ? 'true' : 'false');
    syncPlanner();
    state.candidateViews.forEach(function (view) { view.sync(); });
    state.proposalViews.forEach(function (model) { model.sync(); });
  }
  function setRetry(label, action) {
    state.retry = action || null;
    ui.retry.hidden = !action;
    if (action) ui.retry.textContent = label;
  }
  function stopPoll() {
    window.clearTimeout(state.timer);
    state.timer = null;
  }
  function schedulePoll() {
    stopPoll();
    if (!activeRun() || state.pollStopped || state.leaving || document.hidden) return;
    state.timer = window.setTimeout(function () { readRun(state.run.id); }, 1000);
  }

  function renderSources() {
    ui['source-list'].replaceChildren();
    state.sources.forEach(function (source) {
      var label = node('label', 'fm-research-source');
      var input = node('input');
      input.type = 'radio';
      input.name = 'research-source';
      input.value = source.key;
      input.checked = state.source === source.key;
      input.disabled = !source.available;
      input.addEventListener('change', function () {
        state.source = source.key;
        updateSourceMode();
        refreshControls();
      });
      var description = node('span');
      description.append(node('strong', '', source.label), node('small', '', source.detail || (source.available ? '可使用' : '尚未配置，不可使用')));
      label.append(input, description);
      ui['source-list'].appendChild(label);
    });
    updateSourceMode();
    refreshControls();
  }
  function updateSourceMode() {
    var source = selectedSource();
    ui['source-mode'].textContent = isRealSource(source) ? '真实联网来源 · 取得范围以结果为准' : '离线资料演示 · 不是实时查询';
    ui['consent-wrap'].hidden = !state.sources.some(function (item) { return item.available && isRealSource(item); });
    ui.consent.checked = state.consent;
  }
  async function loadSources() {
    if (state.loadingSources) return;
    state.loadingSources = true;
    ui['settings-feedback'].textContent = '正在读取来源状态…';
    ui['reload-sources'].hidden = true;
    refreshControls();
    try {
      var data = await request('/research-sources');
      var seen = new Set();
      state.sources = list(data.sources).map(function (source) {
        var key = text(source.id || source.source);
        if (!key || seen.has(key)) return null;
        seen.add(key);
        return {key: key, label: text(source.label || source.name || key), available: source.available === true, is_simulated: key === 'fake' || source.is_simulated === true, detail: text(source.detail)};
      }).filter(Boolean);
      if (!seen.has('brave')) state.sources.push({key: 'brave', label: 'Brave 网页搜索', available: false, is_simulated: false, detail: '服务端尚未配置，不可使用；无需在此输入密钥。'});
      if (!seen.has('fake')) state.sources.unshift({key: 'fake', label: '离线模拟资料', available: false, is_simulated: true, detail: '模拟来源当前不可用。'});
      var preferred = state.sources.find(function (source) { return source.key === 'public_web' && source.available; });
      state.source = preferred ? preferred.key : 'fake';
      state.consent = data.consent_enabled === true;
      state.sourcesReady = true;
      ui['settings-feedback'].textContent = '';
      renderSources();
    } catch (error) {
      state.sourcesReady = false;
      ui['settings-feedback'].textContent = '无法读取资料来源：' + error.message + ' 普通聊天仍可使用。';
      ui['reload-sources'].hidden = false;
    } finally { state.loadingSources = false; refreshControls(); }
  }
  async function updateConsent() {
    var enabled = ui.consent.checked;
    var previous = state.consent;
    state.savingConsent = true;
    ui['settings-feedback'].textContent = '正在保存联网授权…';
    refreshControls();
    try {
      var data = await request('/research-consent', 'POST', {enabled: enabled});
      if (typeof data.enabled !== 'boolean') throw new Error('授权状态未返回，请重试。');
      state.consent = data.enabled;
      ui['settings-feedback'].textContent = state.consent ? '已保存联网授权；选择真实来源后才会联网。' : '已关闭联网授权。';
      if (!state.consent && activeRun() && !state.run.is_simulated) await cancelRun();
    } catch (error) {
      state.consent = previous;
      ui['settings-feedback'].textContent = '授权未保存：' + error.message;
    } finally {
      state.savingConsent = false;
      ui.consent.checked = state.consent;
      refreshControls();
    }
  }

  function validRun(data) { return data && typeof data.id === 'string' && Object.prototype.hasOwnProperty.call(statuses, data.status); }
  function acceptRun(data) {
    if (!validRun(data)) throw new Error('研究任务数据不完整，已停止自动读取。');
    state.unknownRunId = null;
    if (state.run && state.run.id !== data.id) {
      state.candidateViews.clear();
      state.proposalViews.clear();
      resetPlanner();
      ui.candidates.replaceChildren();
    }
    state.run = data;
    state.pollStopped = false;
    remember(data.id);
    renderRun();
    if (terminal.has(data.status)) {
      stopPoll();
      if (['failed', 'interrupted', 'cancelled'].includes(data.status)) {
        setRetry('按当前输入重新检索', function () { startResearch(); });
      } else setRetry('', null);
    } else {
      setRetry('', null);
      schedulePoll();
    }
    refreshControls();
  }
  async function readRun(id) {
    if (state.polling || state.leaving) return;
    state.polling = true;
    var version = state.readVersion;
    try {
      var data = await request('/research/' + encodeURIComponent(id));
      if (version !== state.readVersion) return;
      if (data.id !== id) throw new Error('任务编号不一致，已停止自动读取。');
      acceptRun(data);
      ui['run-feedback'].textContent = '';
    } catch (error) {
      if (version !== state.readVersion) return;
      stopPoll();
      state.pollStopped = true;
      if ([403, 404, 410].includes(error.status)) {
        remember(null);
        state.run = null;
        state.unknownRunId = null;
        state.uncertainCreate = false;
        state.candidateViews.clear();
        state.proposalViews.clear();
        resetPlanner();
        ui.candidates.replaceChildren();
        ui.documents.hidden = true;
        ui.reply.textContent = '';
        ui.errors.hidden = true;
        ui.stale.hidden = true;
        ui.times.textContent = '';
        ui['run-mode'].textContent = '';
        if (error.status === 403) {
          state.authorizationRequired = true;
          ui.status.textContent = '当前身份无法访问这次研究';
          ui.stage.textContent = '本地任务编号已清除，研究操作已停止。请通过有效邀请链接重新确认成员身份后进入。';
          setRetry('', null);
        } else {
          ui.status.textContent = error.status === 410 ? '研究资料已过期' : '研究记录不存在或已清理';
          ui.stage.textContent = error.status === 410 ? '本地任务编号已清除，可按当前需求重新检索；不会自动重放旧请求。' : '本地任务编号已清除，不会自动重新发起检索。';
          setRetry('按当前输入重新检索', function () { startResearch(); });
        }
      } else {
        setRetry('重试读取状态', function () { state.pollStopped = false; return readRun(id); });
      }
      ui['run-feedback'].textContent = error.message + ' 自动轮询已停止。';
    } finally {
      state.polling = false;
      state.restoring = false;
      refreshControls();
      if (version !== state.readVersion && !state.cancelling) schedulePoll();
    }
  }
  async function startResearch(payload) {
    if (state.authorizationRequired || locked() || (!payload && ui.start.disabled) || state.savingConsent || chatForm.getAttribute('aria-busy') === 'true') return;
    var message = messageInput.value.trim();
    if (!payload && !message) {
      messageInput.setCustomValidity('请先写下想查的需求。');
      messageInput.reportValidity();
      messageInput.focus();
      return;
    }
    if (!payload) {
      payload = {message: message, intent: ui.intent.value, scope: ui.scope.value.trim(), enabled_sources: [state.source], consent_version: 'v1', idempotency_key: uuid(), network_consent: !!(isRealSource(selectedSource()) && state.consent)};
    }
    stopPoll();
    state.readVersion += 1;
    state.run = null;
    state.candidateViews.clear();
    state.proposalViews.clear();
    resetPlanner();
    ui.candidates.replaceChildren();
    ui['document-list'].replaceChildren();
    ui.documents.hidden = true;
    ui.reply.textContent = '';
    ui.errors.hidden = true;
    ui.stale.hidden = true;
    ui.times.textContent = '';
    state.creating = true;
    state.lastCreate = payload;
    ui.results.hidden = false;
    ui.status.textContent = '正在创建检索';
    ui.stage.textContent = '只收集参考资料，不修改行程。';
    ui['run-mode'].textContent = payload.enabled_sources.includes('fake') ? '离线资料演示 · 不是实时查询' : '真实联网来源';
    ui['run-feedback'].textContent = '';
    setRetry('', null);
    refreshControls();
    ui.results.focus({preventScroll: true});
    ui.results.scrollIntoView({block: 'start', behavior: 'instant'});
    try {
      var data = await request('/research', 'POST', payload);
      state.uncertainCreate = false;
      if (!validRun(data)) {
        if (data && typeof data.id === 'string') remember(data.id);
        throw new Error('创建结果不完整，请重试同一请求以确认任务状态。');
      }
      acceptRun(data);
    } catch (error) {
      state.uncertainCreate = !error.status || error.status >= 500;
      ui.status.textContent = '未能确认检索已创建';
      ui.stage.textContent = '不会自动重试或重复发起联网请求。';
      ui['run-feedback'].textContent = error.message;
      setRetry('重试同一请求', function () {
        state.uncertainCreate = false;
        return startResearch(state.lastCreate);
      });
    } finally { state.creating = false; refreshControls(); }
  }
  async function cancelRun() {
    if (!activeRun() || state.cancelling) return;
    var id = state.run.id;
    state.cancelling = true;
    state.readVersion += 1;
    stopPoll();
    ui['run-feedback'].textContent = '正在请求取消。已发出的外部请求或费用无法保证撤回。';
    refreshControls();
    try {
      var data = await request('/research/' + encodeURIComponent(id) + '/cancel', 'POST');
      // A read already in flight must not overwrite this newer cancel response.
      if (validRun(data) && data.id === id) acceptRun(data);
      else {
        state.pollStopped = false;
        schedulePoll();
      }
      ui['run-feedback'].textContent = activeRun() ? '取消请求已发送，正在等待任务停止。' : '任务已停止；已取得的资料仍可查看。';
    } catch (error) {
      state.pollStopped = true;
      stopPoll();
      ui['run-feedback'].textContent = '未能确认任务已取消：' + error.message + ' 自动轮询已停止，可重试取消。';
      setRetry('重试读取状态', function () { state.pollStopped = false; return readRun(id); });
    } finally { state.cancelling = false; refreshControls(); }
  }

  function renderRun() {
    var run = state.run;
    ui.results.hidden = false;
    ui.status.textContent = statuses[run.status];
    ui.stage.textContent = text(run.stage) || (terminal.has(run.status) ? '本轮已结束，不再自动读取状态。' : '等待服务返回进度…');
    ui['run-mode'].textContent = run.is_simulated ? '离线资料演示 · 不是实时查询' : '外部资料 · 不等于已核实';
    ui.stale.hidden = !run.context_stale;
    ui.times.textContent = '创建：' + dateLabel(run.created_at) + '；保留截止：' + dateLabel(run.expires_at) + (expiredRun() ? '（已过期，请重新检索）' : '');
    ui.reply.textContent = text(run.reply);
    ui.errors.replaceChildren();
    list(run.errors).forEach(function (error) {
      ui.errors.appendChild(node('li', '', text(error.source) + (error.code ? '（' + text(error.code) + '）' : '') + '：' + text(error.message)));
    });
    ui.errors.hidden = !ui.errors.childElementCount;
    // Render actionable cards only once results are final; polling cannot erase focus or feedback.
    if (terminal.has(run.status)) {
      renderCandidates();
      renderDocuments();
      renderPlanner();
      state.proposalViews.forEach(function (model) { model.sync(); });
    }
  }
  function documentCard(documentData) {
    var article = node('article', 'fm-research-document');
    article.appendChild(node('h4', '', text(documentData.title) || '未命名资料'));
    var scope = scopeLabels[documentData.content_scope] || '取得范围未知';
    article.appendChild(node('p', 'fm-research-help', (documentData.is_simulated ? '模拟资料；' : '外部资料，尚未核实；') + '平台：' + (text(documentData.platform) || '未知') + '；取得范围：' + scope + (documentData.kind ? '；类型：' + text(documentData.kind) : '')));
    article.appendChild(node('p', 'fm-research-help', '发布：' + dateLabel(documentData.published_at) + '；读取：' + dateLabel(documentData.fetched_at)));
    if (documentData.excerpt) article.appendChild(node('p', '', documentData.excerpt));
    article.appendChild(sourceLink(documentData.url, '查看原始来源（新窗口）'));
    return article;
  }
  function renderDocuments() {
    var documents = list(state.run.documents);
    ui.documents.hidden = !documents.length;
    if (!documents.length) return;
    var counts = {body: 0, snippet: 0, link: 0, structured: 0};
    documents.forEach(function (documentData) {
      if (Object.prototype.hasOwnProperty.call(counts, documentData.content_scope)) counts[documentData.content_scope] += 1;
    });
    ui['documents-summary'].textContent = '本轮资料依据：正文 ' + counts.body + ' 条，摘要 ' + counts.snippet + ' 条，仅链接 ' + counts.link + ' 条，地图记录 ' + counts.structured + ' 条';
    ui['document-list'].replaceChildren();
    documents.forEach(function (documentData) { ui['document-list'].appendChild(documentCard(documentData)); });
  }
  function renderCandidates() {
    var candidates = list(state.run.candidates);
    if (!candidates.length) {
      if (!ui.candidates.childElementCount) ui.candidates.appendChild(node('p', 'fm-research-help', '本轮没有可展示的候选。请调整需求后，手动发起新的检索。'));
      return;
    }
    candidates.forEach(function (candidate) {
      var key = text(candidate.id);
      if (!key || state.candidateViews.has(key)) return;
      var view = makeCandidate(candidate);
      state.candidateViews.set(key, view);
      ui.candidates.appendChild(view.card);
    });
    state.candidateViews.forEach(function (view) { view.sync(); });
  }
  function makeCandidate(candidate) {
    var view = {candidate: candidate, busy: false, saved: candidate.saved === true, dismissed: candidate.status === 'dismissed', built: false, keys: new Map(), daySelect: null};
    var card = node('article', 'fm-research-candidate');
    view.card = card;
    card.appendChild(node('h3', '', text(candidate.name) || '未命名候选'));
    var meta = node('div', 'fm-research-meta');
    meta.append(node('span', '', candidate.category === 'meal' ? '餐饮' : candidate.category === 'activity' ? '活动' : '候选资料'), node('span', '', text(candidate.area) || '区域待核实'), node('span', '', candidate.match_status === 'simulated' ? '模拟候选 · 地点未核实' : '地点未消歧'));
    card.appendChild(meta);
    card.appendChild(node('p', '', '适合的理由：' + (text(candidate.reason) || '资料不足，尚无明确推荐理由。')));
    card.appendChild(node('p', 'fm-research-help', '费用未知；未核实具体分店、营业与预约信息，不包含路线或费用承诺。'));
    var risks = list(candidate.risks);
    if (risks.length) {
      var riskList = node('ul', 'fm-research-risks');
      risks.forEach(function (risk) { riskList.appendChild(node('li', '', risk)); });
      card.appendChild(riskList);
    } else card.appendChild(node('p', 'fm-research-help', '暂无已记录的风险，不代表安全或已核实。'));
    if (candidate.source_url) card.appendChild(sourceLink(candidate.source_url, '查看候选来源（新窗口）'));

    var evidence = node('details', 'fm-research-evidence');
    var evidenceIds = new Set(list(candidate.evidence_ids).map(text));
    var documents = list(state.run.documents).filter(function (documentData) { return evidenceIds.has(text(documentData.id)); });
    evidence.appendChild(node('summary', '', '查看依据（' + documents.length + ' 条可展示资料）'));
    if (documents.length) documents.forEach(function (documentData) { evidence.appendChild(documentCard(documentData)); });
    else evidence.appendChild(node('p', 'fm-research-help', evidenceIds.size ? '本轮返回了依据引用，但没有可直接关联的文档，不补造来源。' : '本轮没有提供可关联的依据；请保留待核实状态。'));
    card.appendChild(evidence);

    var actions = node('div', 'fm-research-actions');
    view.feedback = node('p', 'fm-research-feedback');
    view.feedback.setAttribute('role', 'status');
    view.feedback.setAttribute('aria-live', 'polite');
    view.saveButton = button('保存为灵感', function () { selectCandidate(view, 'save'); });
    view.dismissButton = button('本次不考虑', function () { selectCandidate(view, 'dismiss'); });
    actions.append(view.saveButton, view.dismissButton);
    card.append(actions, view.feedback);
    view.builder = node('div', 'fm-research-builder');
    view.builder.appendChild(node('p', 'fm-research-help', candidate.match_status === 'simulated' ? '仅可作为待核实备注加入某天，不含路线/费用承诺。生成后仍需确认，不是正式安排。' : '候选线索不等于已核实地点。可保存为灵感，或在上方选择资料生成待确认行程草案，不自动放入行程。'));
    if (candidate.match_status === 'simulated') {
      view.dayArea = node('div');
      view.loadDaysButton = button('选择已有天数，生成备注提案', function () { showDayPicker(view); });
      view.builder.append(view.loadDaysButton, view.dayArea);
    }
    card.appendChild(view.builder);
    view.proposalArea = node('div');
    card.appendChild(view.proposalArea);
    view.sync = function () {
      view.saveButton.disabled = state.pendingActions > 0 || view.busy || view.saved || view.dismissed;
      view.dismissButton.disabled = state.pendingActions > 0 || view.busy || view.dismissed || view.built;
      view.saveButton.textContent = view.saved ? '已保存为灵感' : '保存为灵感';
      view.dismissButton.textContent = view.dismissed ? '本次已忽略' : '本次不考虑';
      if (view.loadDaysButton) view.loadDaysButton.disabled = view.busy || view.dismissed || view.built || !canBuild(candidate);
      if (view.buildButton) view.buildButton.disabled = view.busy || view.dismissed || view.built || !canBuild(candidate) || !view.daySelect.value;
      if (view.daySelect) view.daySelect.disabled = view.busy || view.dismissed || view.built || !canBuild(candidate);
    };
    view.sync();
    return view;
  }
  async function loadDays() {
    if (state.days) return state.days;
    if (!state.daysRequest) {
      state.daysRequest = request('/days').then(function (days) {
        if (!Array.isArray(days)) throw new Error('未能读取已有天数，请重试。');
        state.days = days.filter(function (day) { return typeof day.id === 'string' && day.id; });
        return state.days;
      }).finally(function () { state.daysRequest = null; });
    }
    return state.daysRequest;
  }
  function planLink() {
    var link = node('a', '', '前往行程计划创建天数');
    link.href = '/trip/' + encodeURIComponent(trip) + '/plan?member=' + encodeURIComponent(member);
    return link;
  }
  async function showDayPicker(view) {
    if (view.busy || !canBuild(view.candidate)) return;
    view.busy = true;
    view.sync();
    view.feedback.textContent = '正在读取已有天数…';
    state.pendingActions += 1;
    refreshControls();
    try {
      var days = await loadDays();
      view.dayArea.replaceChildren();
      if (!days.length) {
        view.feedback.textContent = '还没有行程天数。请先创建一天，再回到此处刷新天数。';
        view.dayArea.appendChild(planLink());
        state.days = null;
        view.loadDaysButton.textContent = '刷新已有天数';
        return;
      }
      var label = node('label', '', '将待核实备注加入哪一天？');
      var select = node('select');
      var placeholder = node('option', '', '请选择已有的一天');
      placeholder.value = '';
      select.appendChild(placeholder);
      days.forEach(function (day) {
        var option = node('option', '', '第 ' + text(day.day_index) + ' 天' + (day.title ? ' · ' + text(day.title) : '') + (day.date ? '（' + text(day.date) + '）' : ''));
        option.value = day.id;
        select.appendChild(option);
      });
      label.appendChild(select);
      view.daySelect = select;
      select.addEventListener('change', view.sync);
      view.buildButton = button('生成待核实备注提案', function () { selectCandidate(view, 'build_proposal'); });
      view.dayArea.append(label, view.buildButton);
      view.feedback.textContent = '请选择一天；此操作只生成备注提案，确认后才写入。';
      view.loadDaysButton.hidden = true;
      select.focus();
    } catch (error) { view.feedback.textContent = '天数读取失败：' + error.message; }
    finally {
      view.busy = false;
      state.pendingActions -= 1;
      view.sync();
      refreshControls();
    }
  }
  async function selectCandidate(view, action) {
    if (view.busy || view.dismissed || (action === 'save' && view.saved)) return;
    if (action === 'build_proposal' && (!canBuild(view.candidate) || !view.daySelect || !view.daySelect.value || view.built)) return;
    var runId = state.run.id;
    var dayId = action === 'build_proposal' ? view.daySelect.value : '';
    var operation = action + ':' + dayId;
    if (!view.keys.has(operation)) view.keys.set(operation, uuid());
    var payload = {candidate_id: view.candidate.id, action: action, idempotency_key: view.keys.get(operation)};
    if (dayId) payload.day_id = dayId;
    view.busy = true;
    state.pendingActions += 1;
    view.feedback.textContent = action === 'build_proposal' ? '正在生成待核实备注提案…' : action === 'save' ? '正在保存灵感资料…' : '正在记录本次选择…';
    view.sync();
    refreshControls();
    try {
      var data = await request('/research/' + encodeURIComponent(runId) + '/selections', 'POST', payload);
      view.feedback.textContent = text(data.message) || (action === 'save' ? '已保存为灵感，未修改行程。' : action === 'dismiss' ? '本次不再考虑该候选。' : '已返回提案结果。');
      if (action === 'save') {
        view.saved = data.saved === true || !!data.inspiration;
        if (!view.saved) view.feedback.textContent += ' 服务未确认已保存，可重试同一操作。';
      }
      if (action === 'dismiss') view.dismissed = true;
      if (data.proposal && typeof data.proposal.id === 'string') {
        view.built = true;
        renderProposal(view, data.proposal);
      } else if (action === 'build_proposal') view.feedback.textContent += ' 没有收到可确认的提案；没有自动写入行程。';
      if ((action === 'save' && view.saved) || (action === 'build_proposal' && view.built)) {
        // These writes may change the server fingerprint. Check once, never resume a terminal poll.
        try {
          var latest = await request('/research/' + encodeURIComponent(runId));
          if (validRun(latest) && latest.id === runId) acceptRun(latest);
        } catch (_) { view.feedback.textContent += ' 未能刷新旅程条件，后续确认仍由服务端重新校验。'; }
      }
    } catch (error) { view.feedback.textContent = '操作未确认：' + error.message + ' 可再次点击重试，同一操作不会换用新的请求编号。'; }
    finally {
      view.busy = false;
      state.pendingActions -= 1;
      view.sync();
      refreshControls();
    }
  }
  function canPlan() {
    return state.run && state.run.is_simulated === false && ['succeeded', 'partial'].includes(state.run.status) && state.consent && !state.savingConsent && !state.run.context_stale && !expiredRun() && !state.authorizationRequired;
  }
  function resetPlanner() {
    state.planner = null;
    ui.planner.hidden = true;
    ui['plan-fields'].hidden = true;
    ui['plan-output'].replaceChildren();
    ui['plan-feedback'].textContent = '';
    ui['model-consent'].checked = false;
    ui['geo-consent'].checked = false;
  }
  function renderPlanner() {
    ui.planner.hidden = !(state.run && state.run.is_simulated === false && terminal.has(state.run.status));
    if (ui.planner.hidden || state.planner) return;
    state.planner = {runId: state.run.id, days: [], candidates: [], keys: new Map(), busy: false, ready: false, completed: ''};
    if (!['succeeded', 'partial'].includes(state.run.status)) ui['plan-feedback'].textContent = '本轮检索未成功完成，不能生成行程草案。请先处理上方来源错误，再明确重新检索。';
  }
  function planSelection() {
    var planner = state.planner;
    return {candidate_ids: planner.candidates.filter(function (entry) { return entry.input.checked; }).map(function (entry) { return entry.id; }),
      day_ids: planner.days.filter(function (entry) { return entry.input.checked; }).map(function (entry) { return entry.id; }),
      model_consent: ui['model-consent'].checked, geo_consent: ui['geo-consent'].checked};
  }
  function syncPlanner() {
    var planner = state.planner;
    if (!planner) return;
    var selection = planSelection();
    var blocked = !canPlan() || planner.busy || state.pendingActions > 0 || chatForm.getAttribute('aria-busy') === 'true';
    ui['plan-open'].disabled = blocked;
    planner.days.concat(planner.candidates).forEach(function (entry) { entry.input.disabled = blocked; });
    ui['model-consent'].disabled = ui['geo-consent'].disabled = blocked;
    ui['plan-submit'].disabled = blocked || !planner.ready || selection.day_ids.length < 1 || selection.day_ids.length > 3 || selection.candidate_ids.length < 1 || selection.candidate_ids.length > 5 || planner.completed === JSON.stringify(selection);
  }
  function planCheckbox(target, labelText, id, checked) {
    var label = node('label', 'fm-research-plan-check');
    var input = node('input');
    input.type = 'checkbox';
    input.checked = checked;
    input.addEventListener('change', syncPlanner);
    label.append(input, node('span', '', labelText));
    target.appendChild(label);
    return {id: id, input: input};
  }
  async function openPlanner() {
    var planner = state.planner;
    if (!planner || !canPlan() || state.pendingActions) return;
    planner.busy = true;
    state.pendingActions += 1;
    ui['plan-feedback'].textContent = '正在读取已有天数…';
    refreshControls();
    try {
      state.days = null;
      var days = await loadDays();
      ui['plan-fields'].hidden = false;
      ui['plan-days'].replaceChildren(node('legend', '', '选择已有天数（1–3 天）'));
      ui['plan-candidates'].replaceChildren(node('legend', '', '选择资料候选（1–5 个）'));
      planner.days = days.map(function (day) { return planCheckbox(ui['plan-days'], '第 ' + text(day.day_index) + ' 天' + (day.title ? ' · ' + text(day.title) : ''), day.id, false); });
      planner.candidates = list(state.run.candidates).filter(function (candidate) { return candidate.id && candidate.status !== 'dismissed' && candidate.match_status !== 'simulated'; }).map(function (candidate, index) { return planCheckbox(ui['plan-candidates'], text(candidate.name) || '未命名候选', candidate.id, index < 5); });
      planner.ready = !!days.length && !!planner.candidates.length;
      ui['plan-feedback'].textContent = days.length ? '选择 1–3 个已有天数及 1–5 个候选。公开摘要不等于地点已核实。' : '还没有行程天数，请先创建，再返回刷新天数。';
      if (!days.length) ui['plan-days'].appendChild(planLink());
      ui['plan-open'].textContent = '刷新天数与候选';
    } catch (error) { ui['plan-feedback'].textContent = '天数读取失败：' + error.message; }
    finally { planner.busy = false; state.pendingActions -= 1; refreshControls(); }
  }
  async function buildPlan() {
    var planner = state.planner;
    if (!planner || !canPlan() || ui['plan-submit'].disabled || state.pendingActions) return;
    var selection = planSelection();
    var signature = JSON.stringify(selection);
    if (!planner.keys.has(signature)) planner.keys.set(signature, uuid());
    planner.busy = true;
    state.pendingActions += 1;
    ui['plan-feedback'].textContent = '正在生成草案，可能需要几十秒。尚未修改行程；不会自动重试。';
    refreshControls();
    try {
      var data = await request('/research/' + encodeURIComponent(planner.runId) + '/plan', 'POST', Object.assign({}, selection, {idempotency_key: planner.keys.get(signature)}), 60000);
      if (!data.proposal || typeof data.proposal.id !== 'string') throw new Error('未收到可确认的提案，不能视为行程已生成。');
      if (!state.proposalViews.has(data.proposal.id)) renderPlanning(data);
      renderProposal({proposalArea: ui['plan-output']}, data.proposal, 'plan');
      planner.completed = signature;
      ui['plan-feedback'].textContent = text(data.message) || '已生成待确认草案，尚未修改行程。';
    } catch (error) { ui['plan-feedback'].textContent = '草案未确认：' + error.message + ' 不会自动重试；如需重试，请再次点击，同一选择将复用请求编号。'; }
    finally { planner.busy = false; state.pendingActions -= 1; refreshControls(); }
  }
  function renderPlanning(data) {
    var planning = data.planning || {};
    var output = ui['plan-output'];
    output.appendChild(node('h4', '', text(data.summary) || '行程草案预览'));
    output.appendChild(node('p', 'fm-research-help', planning.model_mode === 'deepseek' ? 'DeepSeek 仅从选中公开资料提取有字面依据的地点；时间由本地规则安排，不是完整 AI 规划或核实。' : '本地规则抽取与排时；未使用模型，不是 AI 规划。'));
    var places = node('ul', 'fm-research-plan-preview');
    list(planning.places).forEach(function (place) {
      var day = (state.days || []).find(function (entry) { return entry.id === place.day_id; });
      var item = node('li');
      item.appendChild(node('strong', '', (day ? '第 ' + text(day.day_index) + ' 天' : '所选天数') + ' · ' + text(place.name)));
      item.appendChild(node('p', '', '草案时间：' + (text(place.start_time) || '未定') + '–' + (text(place.end_time) || '未定') + '；建议停留：' + (place.stay_minutes == null ? '未定' : text(place.stay_minutes) + ' 分钟') + '；地点匹配：' + ({matched: '已匹配（仍待核实）', map_matched: '地图同城同名匹配（不代表营业等已核实）', unresolved: '未消歧', ambiguous: '存在歧义'}[place.match_status] || '状态未知，仍待核实')));
      if (place.reason) item.appendChild(node('p', '', place.reason));
      var ids = new Set(list(place.evidence_ids).map(text));
      var evidence = list(state.run.documents).filter(function (documentData) { return ids.has(text(documentData.id)); });
      if (evidence.length) {
        var details = node('details');
        details.appendChild(node('summary', '', '查看草案依据（' + evidence.length + ' 条）'));
        evidence.forEach(function (documentData) { details.appendChild(documentCard(documentData)); });
        item.appendChild(details);
      } else item.appendChild(node('p', 'fm-research-help', '未返回可关联的文档，不能视为已有核实依据。'));
      places.appendChild(item);
    });
    output.appendChild(places);
    var warnings = node('ul', 'fm-research-risks');
    list(planning.warnings).forEach(function (warning) { warnings.appendChild(node('li', '', warning)); });
    if (warnings.childElementCount) output.appendChild(warnings);
    var budget = planning.budget || {};
    output.appendChild(node('p', 'fm-research-help', '预算参考：已知合计 ' + (budget.known_total == null ? '未知' : text(budget.known_total)) + '；未知费用项 ' + (Array.isArray(budget.unknown_items) ? budget.unknown_items.length : budget.unknown_items == null ? '未知' : text(budget.unknown_items)) + '；旅程预算 ' + (budget.trip_budget == null ? '未设置' : text(budget.trip_budget)) + '。' + text(budget.note) + ' 未知费用不等于免费，不能据此保证预算满足。'));
    if (list(planning.routes).length) {
      output.appendChild(node('p', 'fm-research-help', '路线参考（不代表现场通行、安全或时间承诺）：'));
      list(planning.routes).forEach(function (route) { output.appendChild(node('p', 'fm-research-help', outcomeText(route))); });
    } else output.appendChild(node('p', 'fm-research-help', '未返回路线结果，不代表地点之间可按草案时间到达。'));
  }

  function renderProposal(view, proposal, kind) {
    if (state.proposalViews.has(proposal.id)) return;
    var isPlan = kind === 'plan';
    var box = node('section', 'fm-research-proposal');
    box.setAttribute('aria-label', isPlan ? '真实资料行程草案提案' : '待核实备注提案');
    box.appendChild(node('h4', '', text(proposal.summary) || (isPlan ? '待确认行程草案' : '待核实候选备注提案')));
    box.appendChild(node('p', 'fm-research-help', isPlan ? '这是基于真实资料的行程草案，活动不是已核实安排。营业、票价与安全需求仍待核实；时间由本地规则安排，不保证满足偏好、预算或路线可行性。确认后才修改行程。' : '这是待核实候选备注，不是已核实地点或正式安排；不含路线、时间或费用承诺。'));
    var impacts = node('ul');
    list(proposal.impacts).forEach(function (impact) {
      var parts = [];
      list(impact.diffs).forEach(function (diff) { parts.push(text(diff.field) + '：' + (text(diff.before) || '（空）') + ' → ' + (text(diff.after) || '（空）')); });
      list(impact.impact_notes).forEach(function (note) { parts.push(text(note)); });
      if (impact.blocked_reason) parts.push('不可执行：' + text(impact.blocked_reason));
      impacts.appendChild(node('li', '', parts.join('\n') || '未提供详细影响；请核实后再确认。'));
    });
    if (!impacts.childElementCount) impacts.appendChild(node('li', '', '未提供详细影响；请核实后再确认。'));
    box.appendChild(impacts);
    var actions = node('div', 'fm-research-actions');
    var feedback = node('p', 'fm-research-feedback');
    feedback.setAttribute('role', 'status');
    feedback.setAttribute('aria-live', 'polite');
    var model = {proposal: proposal, isPlan: isPlan, busy: false, done: proposal.status !== 'pending', feedback: feedback, actions: actions};
    var confirm = button(isPlan ? '确认执行行程草案' : '确认加入待核实备注', function () { proposalAction(model, 'confirm'); });
    var reject = button('拒绝提案', function () { proposalAction(model, 'reject'); });
    actions.append(confirm, reject);
    box.append(actions, feedback);
    model.sync = function () {
      confirm.disabled = state.pendingActions > 0 || model.busy || model.done || !!state.run.context_stale || expiredRun();
      reject.disabled = state.pendingActions > 0 || model.busy || model.done;
      actions.hidden = model.done;
    };
    if (model.done) feedback.textContent = proposal.status === 'rejected' ? '该提案已拒绝，未写入行程。' : '该提案已处理；当前响应未提供逐项执行明细，请在行程计划或变更历史核对。';
    model.sync();
    state.proposalViews.set(proposal.id, model);
    view.proposalArea.appendChild(box);
  }
  function outcomeText(value) {
    if (typeof value === 'string') return value;
    if (!value || typeof value !== 'object') return text(value);
    if (value.reason || value.message) return text(value.reason || value.message);
    return JSON.stringify(value);
  }
  async function proposalAction(model, action) {
    if (state.pendingActions > 0 || model.busy || model.done || (action === 'confirm' && (state.run.context_stale || expiredRun()))) return;
    model.busy = true;
    state.pendingActions += 1;
    model.feedback.textContent = action === 'confirm' ? '正在确认，请勿重复操作…' : '正在拒绝提案…';
    model.sync();
    refreshControls();
    try {
      var data = await request('/proposals/' + encodeURIComponent(model.proposal.id) + '/' + action, 'POST');
      model.done = true;
      if (action === 'reject') model.feedback.textContent = '已拒绝提案，未修改行程。';
      else {
        var executed = list(data.executed);
        var skipped = list(data.skipped);
        var lines = ['已执行 ' + executed.length + ' 项；跳过 ' + skipped.length + ' 项。'];
        executed.forEach(function (item) { lines.push('已执行：' + outcomeText(item)); });
        skipped.forEach(function (item) { lines.push('跳过：' + outcomeText(item)); });
        if (!executed.length) lines.push('没有已执行项，不代表提案已成功写入行程。');
        else lines.push(model.isPlan ? '草案已按执行结果写入；活动及其营业、票价、安全需求仍待核实。' : '加入的仍是待核实备注，不是已核实地点或正式安排。');
        model.feedback.textContent = lines.join('\n');
      }
    } catch (error) { model.feedback.textContent = '未能确认操作结果：' + error.message + ' 可重试；请勿把未确认结果视为已写入。'; }
    finally {
      model.busy = false;
      state.pendingActions -= 1;
      model.sync();
      refreshControls();
    }
  }

  ui['plan-open'].addEventListener('click', openPlanner);
  ui['plan-submit'].addEventListener('click', buildPlan);
  ui['model-consent'].addEventListener('change', syncPlanner);
  ui['geo-consent'].addEventListener('change', syncPlanner);
  ui.start.addEventListener('click', function () { startResearch(); });
  ui.cancel.addEventListener('click', cancelRun);
  ui.retry.addEventListener('click', async function () {
    if (!state.retry || ui.retry.disabled) return;
    ui.retry.disabled = true;
    try { await state.retry(); } finally { ui.retry.disabled = false; }
  });
  ui['reload-sources'].addEventListener('click', loadSources);
  ui.consent.addEventListener('change', updateConsent);
  messageInput.addEventListener('input', function () { messageInput.setCustomValidity(''); });
  document.addEventListener('fm-chat-state', refreshControls);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopPoll();
    else if (activeRun() && !state.pollStopped && !state.polling) schedulePoll();
  });
  window.addEventListener('pagehide', function () { state.leaving = true; stopPoll(); });
  window.addEventListener('pageshow', function () { state.leaving = false; schedulePoll(); });

  loadSources();
  var storedId = null;
  try { storedId = window.localStorage.getItem(storageKey); } catch (_) { /* No persistent storage. */ }
  if (storedId) {
    state.unknownRunId = storedId;
    state.restoring = true;
    ui.results.hidden = false;
    ui.status.textContent = '正在恢复上次研究';
    ui.stage.textContent = '只读取已有任务，不会重新发起联网。';
    refreshControls();
    readRun(storedId);
  }
})();
