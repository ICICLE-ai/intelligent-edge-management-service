/**
 * Shared HLS.js attach helpers for device test streams, deployment inference,
 * and group CCTV grids.
 */
(function (global) {
  'use strict';

  var DEFAULT_OPTS = {
    lowLatencyMode: true,
    liveSyncDurationCount: 2,
    liveMaxLatencyDurationCount: 5,
    maxLiveSyncPlaybackRate: 1.25,
    backBufferLength: 0,
    manifestLoadingMaxRetry: 6,
  };

  function mergeOpts(options) {
    var out = {};
    var key;
    for (key in DEFAULT_OPTS) {
      if (Object.prototype.hasOwnProperty.call(DEFAULT_OPTS, key)) {
        out[key] = DEFAULT_OPTS[key];
      }
    }
    options = options || {};
    for (key in options) {
      if (Object.prototype.hasOwnProperty.call(options, key)) {
        out[key] = options[key];
      }
    }
    return out;
  }

  function attachOne(video, url, statusEl, options) {
    if (!video || !url) return null;

    var hls = null;
    var retryTimer = null;
    var opts = mergeOpts(options);
    var waitingMsg = opts.waitingMessage || 'Waiting for stream…';
    var connectingMsg = opts.connectingMessage || 'Connecting…';

    function setStatus(msg) {
      if (statusEl) statusEl.textContent = msg;
    }

    function scheduleRetry() {
      if (retryTimer) return;
      setStatus(waitingMsg);
      retryTimer = setTimeout(function () {
        retryTimer = null;
        start();
      }, opts.retryMs || 4000);
    }

    function start() {
      if (global.Hls && global.Hls.isSupported()) {
        if (hls) {
          hls.destroy();
          hls = null;
        }
        hls = new global.Hls(opts);
        hls.loadSource(url);
        hls.attachMedia(video);
        hls.on(global.Hls.Events.MANIFEST_PARSED, function () {
          setStatus('Live');
          video.play().catch(function () {});
        });
        hls.on(global.Hls.Events.ERROR, function (evt, data) {
          if (!data || !data.fatal) return;
          if (data.type === global.Hls.ErrorTypes.MEDIA_ERROR && hls) {
            hls.recoverMediaError();
            return;
          }
          if (hls) {
            hls.destroy();
            hls = null;
          }
          scheduleRetry();
        });
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = url;
        video.addEventListener('loadedmetadata', function () {
          setStatus('Live');
        });
        video.addEventListener('error', scheduleRetry);
      } else {
        setStatus('This browser cannot play HLS.');
      }
    }

    setStatus(connectingMsg);
    start();

    return {
      destroy: function () {
        if (retryTimer) {
          clearTimeout(retryTimer);
          retryTimer = null;
        }
        if (hls) {
          hls.destroy();
          hls = null;
        }
      },
    };
  }

  function attachAll(root, selector, options) {
    if (!root) return [];
    var nodes = root.querySelectorAll(selector || '[data-hls-url]');
    var players = [];
    var i;
    for (i = 0; i < nodes.length; i++) {
      var tile = nodes[i];
      var url = tile.dataset.hlsUrl;
      if (!url) continue;
      var active = tile.dataset.streamActive !== 'false';
      if (!active) continue;
      var video = tile.querySelector('video');
      var statusEl = tile.querySelector('[data-hls-status]');
      var player = attachOne(video, url, statusEl, options);
      if (player) {
        tile.dataset.hlsAttached = '1';
        tile._hlsPlayer = player;
        players.push(player);
      }
    }
    return players;
  }

  function inferenceBadgeClass(status) {
    var v = String(status || '').toUpperCase();
    if (v === 'RUNNING') return 'badge--green';
    if (['STARTING', 'DELIVERING', 'DOWNLOADING', 'PULLING', 'PENDING'].indexOf(v) >= 0) {
      return 'badge--yellow';
    }
    if (v === 'FAILED') return 'badge--red';
    if (v === 'STOPPED') return 'badge--grey';
    return 'badge--neutral';
  }

  function setTileLive(tile, isRunning) {
    if (!tile) return;
    var placeholder = tile.querySelector('[data-inference-placeholder]');
    var video = tile.querySelector('[data-inference-video]');
    if (isRunning) {
      tile.classList.add('cctv-tile--live');
      tile.dataset.streamActive = 'true';
      if (placeholder) placeholder.style.display = 'none';
      if (video) video.style.display = 'block';
    } else {
      tile.classList.remove('cctv-tile--live');
      tile.dataset.streamActive = 'false';
      if (placeholder) placeholder.style.display = '';
      if (video) video.style.display = 'none';
    }
  }

  function inferenceTilesForDevice(streamRoot, row) {
    var uid = row.device_uid;
    if (!uid) return [];
    var cam = row.camera_index;
    if (cam !== undefined && cam !== null && cam !== '') {
      var one = streamRoot.querySelector(
        '.cctv-tile[data-device-uid="' + uid + '"][data-camera-index="' + cam + '"]'
      );
      return one ? [one] : [];
    }
    return Array.prototype.slice.call(
      streamRoot.querySelectorAll('.cctv-tile[data-device-uid="' + uid + '"]')
    );
  }

  function syncOneInferenceTile(tile, row, options) {
    var status = String(row.status || '').toUpperCase();
    var isRunning = status === 'RUNNING';
    var badge = tile.querySelector('[data-inference-badge]');

    if (badge) {
      badge.textContent = status;
      badge.className = 'badge ' + inferenceBadgeClass(status);
      badge.dataset.status = status;
    }

    setTileLive(tile, isRunning);
    var statusEl = tile.querySelector('[data-hls-status]');
    var video = tile.querySelector('[data-inference-video]');

    if (isRunning) {
      if (statusEl) statusEl.textContent = 'Connecting…';

      if (!tile.dataset.hlsAttached) {
        // Keep per-tile HLS URLs from SSR (multi-cam); poll payload is per-device.
        var url = tile.dataset.hlsUrl;
        if (row.hls_url && row.camera_index !== undefined && row.camera_index !== null && row.camera_index !== '') {
          url = row.hls_url;
          tile.dataset.hlsUrl = url;
        }
        var player = attachOne(video, url, statusEl, options);
        if (player) {
          tile.dataset.hlsAttached = '1';
          tile._hlsPlayer = player;
        }
      }
    } else {
      if (statusEl) statusEl.textContent = '\u00a0';
      if (tile.dataset.hlsAttached && tile._hlsPlayer) {
        tile._hlsPlayer.destroy();
        tile._hlsPlayer = null;
        delete tile.dataset.hlsAttached;
      }
    }
  }

  /** Activate HLS tiles when poll reports RUNNING (no page refresh needed). */
  function syncInferenceTiles(streamRoot, devices, options) {
    if (!streamRoot || !devices || !devices.length) return;
    options = options || {};

    devices.forEach(function (row) {
      var tiles = inferenceTilesForDevice(streamRoot, row);
      tiles.forEach(function (tile) {
        syncOneInferenceTile(tile, row, options);
      });
    });
  }

  function bootstrapInference(streamRoot, options) {
    if (!streamRoot) return;
    var tiles = streamRoot.querySelectorAll('.cctv-tile');
    var i;
    for (i = 0; i < tiles.length; i++) {
      var tile = tiles[i];
      var isLive = tile.classList.contains('cctv-tile--live')
        || tile.dataset.streamActive === 'true';
      if (isLive) setTileLive(tile, true);
      if (!isLive || tile.dataset.hlsAttached) continue;
      var video = tile.querySelector('[data-inference-video]');
      var statusEl = tile.querySelector('[data-hls-status]');
      var player = attachOne(video, tile.dataset.hlsUrl, statusEl, options);
      if (player) {
        tile.dataset.hlsAttached = '1';
        tile._hlsPlayer = player;
      }
    }
  }

  global.IcicleHls = {
    attachOne: attachOne,
    attachAll: attachAll,
    syncInferenceTiles: syncInferenceTiles,
    bootstrapInference: bootstrapInference,
  };
})(window);
