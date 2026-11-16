'use strict';

(function () {
  function qs(sel, root) {
    return (root || document).querySelector(sel);
  }

  function makeEl(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  var els = {
    connBanner: qs('#conn-banner'),
    oscBanner: qs('#osc-banner'),
    nowTitle: qs('#now-title'),
    playingBadge: qs('#playing-badge'),
    clockElapsed: qs('#clock-elapsed'),
    clockRemaining: qs('#clock-remaining'),
    btnStop: qs('#btn-stop'),
    btnToggle: qs('#btn-toggle'),
    autoNext: qs('#auto-next'),
    songCount: qs('#song-count'),
    songList: qs('#song-list'),
    trackList: qs('#track-list'),
    trackCount: qs('#track-count'),
    autoHideDefaults: qs('#auto-hide-defaults'),
    btnHiddenTracks: qs('#btn-hidden-tracks'),
    hiddenCount: qs('#hidden-count'),
    hiddenTrackList: qs('#hidden-track-list'),
    btnGlobalSettings: qs('#btn-global-settings'),
    drawerSong: qs('#drawer-song'),
    drawerSongTitle: qs('#drawer-song-title'),
    songLimit: qs('#song-limit'),
    songMuteTracks: qs('#song-mute-tracks'),
    btnSongRestore: qs('#btn-song-restore'),
    btnSongSave: qs('#btn-song-save'),
    drawerGlobal: qs('#drawer-global'),
    globalLimit: qs('#global-limit'),
    globalMuteTracks: qs('#global-mute-tracks'),
    globalAutoNext: qs('#global-auto-next'),
    globalPlayOnSwitch: qs('#global-play-on-switch'),
    confirmSwitch: qs('#confirm-switch'),
    btnGlobalSave: qs('#btn-global-save'),
    confirmLayer: qs('#confirm-layer'),
    confirmText: qs('#confirm-text'),
    btnConfirmCancel: qs('#btn-confirm-cancel'),
    btnConfirmOk: qs('#btn-confirm-ok')
  };

  var ACTION_LABELS = {
    none: '到时不动作',
    stop: '到时停止',
    stop_mute: '到时停+静音'
  };

  var state = {
    snapshot: null,
    regionsFallback: null,
    confirmEnabled: true,
    hadDisconnect: false,
    reconnectTimer: null,
    reconnectDelay: 1000,
    ws: null,
    songDrawerId: null,
    pendingSongId: null,
    hiddenTracks: {},
    autoHideDefaults: true,
    oscDown: false
  };

  try {
    state.confirmEnabled = localStorage.getItem('pgm_confirm_switch') !== '0';
    state.autoHideDefaults = localStorage.getItem('pgm_auto_hide_defaults') !== '0';
    var savedHidden = localStorage.getItem('pgm_hidden_tracks');
    if (savedHidden) state.hiddenTracks = JSON.parse(savedHidden) || {};
  } catch (e) {
    state.confirmEnabled = true;
    state.autoHideDefaults = true;
    state.hiddenTracks = {};
  }

  /* ---------- 工具 ---------- */

  function fmtClock(seconds, allowNegative) {
    if (seconds === null || seconds === undefined || !isFinite(Number(seconds))) return '–:–';
    var num = Number(seconds);
    var neg = num < 0;
    if (neg && !allowNegative) {
      num = 0;
    } else if (neg) {
      num = -num;
    }
    var total = Math.round(num);
    var m = Math.floor(total / 60);
    var s = total % 60;
    return (neg && allowNegative ? '-' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  function actionLabel(action) {
    return ACTION_LABELS[action] || action;
  }

  function toast(message, type) {
    var box = qs('#toast-box');
    if (!box) {
      box = document.createElement('div');
      box.id = 'toast-box';
      document.body.appendChild(box);
    }
    var item = makeEl('div', 'toast' + (type ? ' toast-' + type : ''), message);
    box.appendChild(item);
    setTimeout(function () { item.remove(); }, 3200);
  }

  function trackNum(track) {
    return track.index + 1;
  }

  function isDefaultTrackName(name) {
    return /^(Track|轨道)\s*\d+$/.test(String(name || '').trim());
  }

  function allTracks() {
    return (state.snapshot && state.snapshot.tracks) || [];
  }

  function visibleTracks() {
    return allTracks().filter(function (track) {
      var num = trackNum(track);
      if (state.hiddenTracks[num]) return false;
      if (state.autoHideDefaults && isDefaultTrackName(track.name)) return false;
      return true;
    });
  }

  function saveHiddenTracks() {
    try {
      localStorage.setItem('pgm_hidden_tracks', JSON.stringify(state.hiddenTracks));
    } catch (e) { /* 忽略 */ }
  }

  function saveAutoHideDefaults() {
    try {
      localStorage.setItem('pgm_auto_hide_defaults', state.autoHideDefaults ? '1' : '0');
    } catch (e) { /* 忽略 */ }
  }

  function manualHiddenTracks() {
    return allTracks().filter(function (track) { return !!state.hiddenTracks[trackNum(track)]; });
  }

  function listRegions() {
    if (state.snapshot && state.snapshot.regions) return state.snapshot.regions;
    if (state.regionsFallback) return state.regionsFallback;
    return [];
  }

  function regionEffective(region) {
    var s = state.snapshot;
    if (s) {
      var custom = s.song_settings && s.song_settings[region.id];
      if (custom) return custom;
      if (s.defaults) return s.defaults;
    }
    if (region && region.limit_s !== undefined) return region;
    return { limit_s: null, end_action: 'none', mute_tracks: [] };
  }

  function summaryOf(region) {
    var eff = regionEffective(region);
    var limitTxt = (eff.limit_s === null || eff.limit_s === undefined) ? '不限' : fmtClock(eff.limit_s);
    var text = limitTxt + ' · ' + actionLabel(eff.end_action);
    if (eff.mute_tracks && eff.mute_tracks.length) {
      text += ' · 静音' + eff.mute_tracks.join(',');
    }
    return text;
  }

  /* ---------- 渲染 ---------- */

  function renderNow() {
    var s = state.snapshot;
    var cs = s && s.current_song;
    els.nowTitle.textContent = cs ? cs.name : '未选曲';

    var clock = s && s.clock ? s.clock : null;
    els.clockElapsed.textContent = (clock && clock.hhmmss) ? clock.hhmmss : fmtClock(clock ? clock.elapsed_s : null);
    els.clockRemaining.textContent = (clock && clock.hhmmss_remaining) ? clock.hhmmss_remaining : fmtClock(clock ? clock.remaining_s : null, true);

    var rem = clock ? clock.remaining_s : null;
    els.clockRemaining.classList.toggle('warn', rem !== null && rem !== undefined && Number(rem) < 10);

    var playing = !!(s && s.transport && s.transport.playing);
    els.playingBadge.textContent = playing ? '播放中' : '已停止';
    els.playingBadge.className = 'playing-badge' + (playing ? ' on' : '');
    els.btnToggle.textContent = playing ? '⏸ 暂停' : '▶ 播放';
  }

  function renderSongs() {
    var regions = listRegions();
    els.songList.innerHTML = '';
    els.songCount.textContent = regions.length + ' 首';
    if (!regions.length) {
      els.songList.appendChild(makeEl('li', 'empty-state', '暂无曲目'));
      return;
    }
    var currentId = (state.snapshot && state.snapshot.current_song) ? state.snapshot.current_song.id : null;
    regions.forEach(function (region) {
      var li = makeEl('li', 'song-row');
      if (region.id === currentId) li.classList.add('current');

      var main = makeEl('button', 'song-main');
      main.type = 'button';
      main.setAttribute('aria-label', '切换到 ' + region.name);
      main.appendChild(makeEl('span', 'song-name', region.name));

      var meta = makeEl('span', 'song-meta');
      meta.appendChild(makeEl('span', null, '全长 ' + fmtClock(region.length_s)));
      meta.appendChild(makeEl('span', null, summaryOf(region)));
      main.appendChild(meta);

      var gear = makeEl('button', 'song-gear', '设置');
      gear.type = 'button';
      gear.setAttribute('aria-label', '设置 ' + region.name);

      main.addEventListener('click', function () { requestSwitchSong(region); });
      gear.addEventListener('click', function (e) { e.stopPropagation(); openSongDrawer(region.id); });

      li.appendChild(main);
      li.appendChild(gear);
      els.songList.appendChild(li);
    });
  }

  function renderTrackMeta() {
    var total = allTracks().length;
    var visible = visibleTracks().length;
    els.trackCount.textContent = total ? ('显示 ' + visible + ' / ' + total + ' 轨') : '';
  }

  function renderHiddenPanel() {
    var manual = manualHiddenTracks();
    els.hiddenCount.textContent = String(manual.length);
    els.btnHiddenTracks.classList.toggle('hidden', manual.length === 0);
    els.hiddenTrackList.innerHTML = '';
    if (!manual.length) {
      els.hiddenTrackList.classList.add('hidden');
      return;
    }
    manual.forEach(function (track) {
      var num = trackNum(track);
      var li = makeEl('li', 'hidden-row');
      li.appendChild(makeEl('span', 'track-name', num + ' ' + track.name));
      var restore = makeEl('button', 'btn-link', '恢复');
      restore.type = 'button';
      restore.addEventListener('click', function () {
        delete state.hiddenTracks[num];
        saveHiddenTracks();
        renderTracks();
        renderHiddenPanel();
        rerenderOpenChips();
      });
      li.appendChild(restore);
      els.hiddenTrackList.appendChild(li);
    });
  }

  function renderTracks() {
    var tracks = visibleTracks();
    els.trackList.innerHTML = '';
    renderTrackMeta();
    renderHiddenPanel();
    if (!tracks.length) {
      els.trackList.appendChild(makeEl('li', 'empty-state', '暂无轨道'));
      return;
    }
    tracks.forEach(function (track) {
      var li = makeEl('li', 'track-row');
      if (track.muted) li.classList.add('muted');
      var btn = makeEl('button', 'track-btn');
      btn.type = 'button';
      btn.appendChild(makeEl('span', 'track-num', String(trackNum(track))));
      btn.appendChild(makeEl('span', 'track-name', track.name));
      btn.appendChild(makeEl('span', 'track-state', track.muted ? '静音' : '正常'));
      btn.setAttribute('aria-label', (track.muted ? '取消静音 ' : '静音 ') + track.name);
      btn.addEventListener('click', function () {
        sendCmd({ cmd: 'set_track_mute', index: trackNum(track), muted: !track.muted });
      });
      li.appendChild(btn);

      var hideBtn = makeEl('button', 'track-hide', '✕');
      hideBtn.type = 'button';
      hideBtn.title = '隐藏轨道';
      hideBtn.setAttribute('aria-label', '隐藏轨道 ' + track.name);
      hideBtn.addEventListener('click', function () {
        var num = trackNum(track);
        state.hiddenTracks[num] = track.name || ('Track ' + num);
        saveHiddenTracks();
        renderTracks();
        rerenderOpenChips();
      });
      li.appendChild(hideBtn);
      els.trackList.appendChild(li);
    });
  }

  function syncAutoNext() {
    var enabled = !!(state.snapshot && state.snapshot.auto_next);
    if (els.autoNext.checked !== enabled) els.autoNext.checked = enabled;
    if (els.globalAutoNext.checked !== enabled) els.globalAutoNext.checked = enabled;
  }

  function applyState(msg) {
    if (!msg || typeof msg !== 'object') return;
    var prev = state.snapshot;
    var prevSongsKey = prev
      ? JSON.stringify(prev.regions) + '|' + JSON.stringify(prev.current_song) + '|' + JSON.stringify(prev.song_settings) + '|' + JSON.stringify(prev.defaults)
      : '';
    var prevTracksKey = prev ? JSON.stringify(prev.tracks) : '';
    state.snapshot = msg;
    renderNow();
    var songsKey = JSON.stringify(msg.regions) + '|' + JSON.stringify(msg.current_song) + '|' + JSON.stringify(msg.song_settings) + '|' + JSON.stringify(msg.defaults);
    if (songsKey !== prevSongsKey) renderSongs();
    if (JSON.stringify(msg.tracks) !== prevTracksKey) renderTracks();
    syncAutoNext();
  }

  /* ---------- 抽屉与确认层 ---------- */

  function updateLock() {
    var anyOpen = !els.drawerSong.classList.contains('hidden') ||
      !els.drawerGlobal.classList.contains('hidden') ||
      !els.confirmLayer.classList.contains('hidden');
    document.body.classList.toggle('lock-scroll', anyOpen);
  }

  function openDrawer(el) {
    el.classList.remove('hidden');
    el.setAttribute('aria-hidden', 'false');
    updateLock();
  }

  function closeDrawer(el) {
    el.classList.add('hidden');
    el.setAttribute('aria-hidden', 'true');
    updateLock();
  }

  function renderMuteChips(container, selected) {
    container.innerHTML = '';
    var tracks = visibleTracks();
    var sel = selected || [];
    if (!tracks.length) {
      container.appendChild(makeEl('p', 'chip-empty', '暂无轨道'));
      return;
    }
    tracks.forEach(function (track) {
      var num = trackNum(track);
      var label = makeEl('label', 'chip');
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = String(num);
      cb.checked = sel.indexOf(num) !== -1;
      label.appendChild(cb);
      label.appendChild(makeEl('span', null, num + ' ' + track.name));
      container.appendChild(label);
    });
  }

  function rerenderOpenChips() {
    if (!els.drawerSong.classList.contains('hidden')) {
      renderMuteChips(els.songMuteTracks, collectChips(els.songMuteTracks));
    }
    if (!els.drawerGlobal.classList.contains('hidden')) {
      renderMuteChips(els.globalMuteTracks, collectChips(els.globalMuteTracks));
    }
  }

  function collectChips(container) {
    var nums = [];
    var boxes = container.querySelectorAll('input[type="checkbox"]');
    Array.prototype.forEach.call(boxes, function (cb) {
      if (cb.checked) nums.push(parseInt(cb.value, 10));
    });
    nums.sort(function (a, b) { return a - b; });
    return nums;
  }

  function parseLimit(input) {
    var raw = input.value.trim();
    if (raw === '') return null;
    var n = Number(raw);
    if (!isFinite(n) || n <= 0) return null;
    return n;
  }

  function checkedRadio(name) {
    var el = qs('input[name="' + name + '"]:checked');
    return el ? el.value : 'none';
  }

  function regionById(id) {
    var regions = listRegions();
    for (var i = 0; i < regions.length; i++) {
      if (regions[i].id === id) return regions[i];
    }
    return null;
  }

  function openSongDrawer(regionId) {
    var region = regionById(regionId);
    if (!region) { toast('找不到该曲目', 'error'); return; }
    state.songDrawerId = regionId;
    var eff = regionEffective(region);
    els.drawerSongTitle.textContent = '《' + region.name + '》设置';
    els.songLimit.value = (eff.limit_s === null || eff.limit_s === undefined) ? '' : String(eff.limit_s);
    var radios = qs('#drawer-song').querySelectorAll('input[name="song-end-action"]');
    Array.prototype.forEach.call(radios, function (r) {
      r.checked = (r.value === (eff.end_action || 'none'));
    });
    renderMuteChips(els.songMuteTracks, eff.mute_tracks || []);
    openDrawer(els.drawerSong);
  }

  function saveSongSettings() {
    if (!state.songDrawerId) return;
    var settings = {
      limit_s: parseLimit(els.songLimit),
      end_action: checkedRadio('song-end-action'),
      mute_tracks: collectChips(els.songMuteTracks)
    };
    if (sendCmd({ cmd: 'set_song_settings', region_id: state.songDrawerId, settings: settings })) {
      closeDrawer(els.drawerSong);
      toast('已保存曲目设置', 'success');
    }
  }

  function restoreSongSettings() {
    if (!state.songDrawerId) return;
    if (sendCmd({ cmd: 'clear_song_settings', region_id: state.songDrawerId })) {
      closeDrawer(els.drawerSong);
      toast('已恢复默认设置', 'success');
    }
  }

  function openGlobalDrawer() {
    var d = (state.snapshot && state.snapshot.defaults) || { limit_s: null, end_action: 'stop_mute', mute_tracks: [], play_on_switch: true };
    els.globalLimit.value = (d.limit_s === null || d.limit_s === undefined) ? '' : String(d.limit_s);
    var radios = qs('#drawer-global').querySelectorAll('input[name="global-end-action"]');
    Array.prototype.forEach.call(radios, function (r) {
      r.checked = (r.value === (d.end_action || 'stop_mute'));
    });
    renderMuteChips(els.globalMuteTracks, d.mute_tracks || []);
    els.globalAutoNext.checked = !!(state.snapshot && state.snapshot.auto_next);
    els.globalPlayOnSwitch.checked = d.play_on_switch !== false;
    els.confirmSwitch.checked = state.confirmEnabled;
    openDrawer(els.drawerGlobal);
  }

  function saveGlobalSettings() {
    var settings = {
      limit_s: parseLimit(els.globalLimit),
      end_action: checkedRadio('global-end-action'),
      mute_tracks: collectChips(els.globalMuteTracks),
      play_on_switch: els.globalPlayOnSwitch.checked
    };
    if (sendCmd({ cmd: 'set_defaults', settings: settings })) {
      closeDrawer(els.drawerGlobal);
      toast('已保存全局设置', 'success');
    }
  }

  function requestSwitchSong(region) {
    if (!region) return;
    if (!state.confirmEnabled) {
      doSwitchSong(region.id);
      return;
    }
    state.pendingSongId = region.id;
    els.confirmText.textContent = '确定切换到《' + region.name + '》？';
    openDrawer(els.confirmLayer);
  }

  function doSwitchSong(regionId) {
    if (!regionId) return;
    var autoplay = !(state.snapshot && state.snapshot.defaults && state.snapshot.defaults.play_on_switch === false);
    sendCmd({ cmd: 'play_song', region_id: regionId, autoplay: autoplay });
    closeDrawer(els.confirmLayer);
    state.pendingSongId = null;
  }

  function cancelSwitch() {
    if (!els.confirmLayer.classList.contains('hidden')) closeDrawer(els.confirmLayer);
    state.pendingSongId = null;
  }

  /* ---------- 连接 ---------- */

  function showBanner() { els.connBanner.classList.remove('hidden'); }
  function hideBanner() { els.connBanner.classList.add('hidden'); }

  function checkOscHealth() {
    fetch('/api/health')
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        var osc = data && data.osc;
        var down = !!(osc && osc.listening === false);
        state.oscDown = down;
        els.oscBanner.classList.toggle('hidden', !down);
      })
      .catch(function () { /* 网络异常时由 WS 横幅提示 */ });
  }

  function scheduleReconnect() {
    showBanner();
    if (state.reconnectTimer) return;
    state.reconnectTimer = setTimeout(function () {
      state.reconnectTimer = null;
      connectWs();
    }, state.reconnectDelay);
    state.reconnectDelay = Math.min(Math.round(state.reconnectDelay * 1.5), 10000);
  }

  function connectWs() {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    var url = proto + location.host + '/ws';
    var ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      scheduleReconnect();
      return;
    }
    state.ws = ws;
    ws.onopen = function () {
      hideBanner();
      state.reconnectDelay = 1000;
      if (state.hadDisconnect) {
        state.hadDisconnect = false;
        loadState();
      }
    };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg && msg.type === 'state') applyState(msg);
      else if (msg && msg.type === 'ack') handleAck(msg);
    };
    ws.onclose = function () {
      if (state.ws !== ws) return;
      state.hadDisconnect = true;
      scheduleReconnect();
    };
    ws.onerror = function () {
      /* 由 onclose 统一处理 */
    };
  }

  function handleAck(msg) {
    if (msg && msg.ok === false) {
      toast('操作失败：' + (msg.error || '未知错误'), 'error');
    }
  }

  function sendCmd(obj) {
    if (!state.ws || state.ws.readyState !== 1) {
      toast('连接已断开，命令未发送', 'error');
      return false;
    }
    state.ws.send(JSON.stringify(obj));
    return true;
  }

  function loadState() {
    return fetch('/api/state')
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) { applyState(data); })
      .catch(function () { /* WS 广播会补齐状态 */ });
  }

  function loadRegions() {
    return fetch('/api/regions')
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        if (data && Array.isArray(data.regions)) {
          state.regionsFallback = data.regions;
          if (!state.snapshot) renderSongs();
        }
      })
      .catch(function () { /* 忽略 */ });
  }

  /* ---------- 事件绑定 ---------- */

  function bindEvents() {
    els.btnStop.addEventListener('click', function () { sendCmd({ cmd: 'stop' }); });
    els.btnToggle.addEventListener('click', function () { sendCmd({ cmd: 'toggle_play' }); });
    els.autoNext.addEventListener('change', function () {
      sendCmd({ cmd: 'set_auto_next', enabled: els.autoNext.checked });
    });
    els.globalAutoNext.addEventListener('change', function () {
      sendCmd({ cmd: 'set_auto_next', enabled: els.globalAutoNext.checked });
    });
    els.btnGlobalSettings.addEventListener('click', openGlobalDrawer);
    els.btnSongSave.addEventListener('click', saveSongSettings);
    els.btnSongRestore.addEventListener('click', restoreSongSettings);
    els.btnGlobalSave.addEventListener('click', saveGlobalSettings);
    els.confirmSwitch.addEventListener('change', function () {
      state.confirmEnabled = els.confirmSwitch.checked;
      try {
        localStorage.setItem('pgm_confirm_switch', state.confirmEnabled ? '1' : '0');
      } catch (e) { /* 忽略 */ }
    });
    els.btnConfirmOk.addEventListener('click', function () { doSwitchSong(state.pendingSongId); });
    els.btnConfirmCancel.addEventListener('click', cancelSwitch);

    els.autoHideDefaults.checked = state.autoHideDefaults;
    els.autoHideDefaults.addEventListener('change', function () {
      state.autoHideDefaults = els.autoHideDefaults.checked;
      saveAutoHideDefaults();
      renderTracks();
      rerenderOpenChips();
    });
    els.btnHiddenTracks.addEventListener('click', function () {
      els.hiddenTrackList.classList.toggle('hidden');
    });

    var closeEls = document.querySelectorAll('[data-close]');
    Array.prototype.forEach.call(closeEls, function (el) {
      el.addEventListener('click', function () {
        closeDrawer(els.drawerSong);
        closeDrawer(els.drawerGlobal);
        cancelSwitch();
      });
    });
  }

  /* ---------- 启动 ---------- */

  function init() {
    bindEvents();
    els.confirmSwitch.checked = state.confirmEnabled;
    renderNow();
    renderSongs();
    renderTracks();
    syncAutoNext();
    loadState();
    loadRegions();
    connectWs();
    checkOscHealth();
    setInterval(checkOscHealth, 10000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
