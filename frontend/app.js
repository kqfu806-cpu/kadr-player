/* Курымдык — клиент плеера. Чистый JS, без сборки. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  const favoritesKey = "kadr-favorites";
  const followedArtistsKey = "kurymdyk-followed-artists";
  const weeklyReleasesKey = "kurymdyk-weekly-releases";
  function readFavorites() {
    try {
      const value = JSON.parse(localStorage.getItem(favoritesKey) || "[]");
      return new Set(Array.isArray(value) ? value.filter((id) => typeof id === "string") : []);
    } catch (_) {
      return new Set();
    }
  }

  function readFollowedArtists() {
    try {
      const value = JSON.parse(localStorage.getItem(followedArtistsKey) || "[]");
      return new Set(Array.isArray(value) ? value.filter((name) => typeof name === "string") : []);
    } catch (_) {
      return new Set();
    }
  }

  const state = {
    tracks: [],
    filtered: [],
    index: -1,
    folder: null,
    playing: false,
    shuffle: false,
    repeat: "off", // off | all | one
    volume: 0.85,
    muted: false,
    media: null, // ответ /api/resolve
    mode: "audio", // audio | youtube
    yt: null,
    ytReady: false,
    seeking: false,
    resolving: false,
    order: [],
    lyrics: null,
    lyricIndex: -1,
    lyricFollow: true,
    lyricUserScroll: false,
    deferredInstall: null,
    beat: 0,
    collapsed: false,
    virtRaf: 0,
    playGen: 0,
    playSession: null,
    waveMode: false,
    waveQueue: [],
    waveSeen: new Set(),
    waveReason: "",
    waveLoading: false,
    lyricPauseUntil: 0,
    coverScrollLock: false,
    coverScrollTimer: 0,
    artists: [],
    artistsLoaded: false,
    followedArtists: readFollowedArtists(),
    releaseTab: "tracks",
    sideTab: "tracks",
    artistFilter: "",
    favorites: readFavorites(),
    favoritesOnly: false,
    _ollamaWarned: false,
  };

  const clipCache = new Map();
  const coverCache = new Map();
  const lyricsCache = new Map();
  const resolvingInFlight = new Set();
  const coverInFlight = new Set();
  const libraryPathKey = "kadr-library-path";
  const coverLsKey = "kadr-covers";
  function coverLsAll() {
    try { return JSON.parse(localStorage.getItem(coverLsKey) || "{}"); } catch (_) { return {}; }
  }
  function coverUrlOf(data) {
    if (!data || !data.cover_file) return "";
    return `/api/cover/${encodeURIComponent(data.cover_file)}`;
  }
  function coverLsGet(id) {
    const hit = coverCache.get(id);
    if (hit && hit.cover_file) return coverUrlOf(hit);
    const all = coverLsAll();
    const v = all[id];
    if (!v) return "";
    if (v.indexOf("ph:") === 0) return `/api/cover/${encodeURIComponent(v.slice(3))}`;
    return v;
  }
  function coverLsSet(id, url, placeholder) {
    if (!id || !url) return;
    try {
      const all = coverLsAll();
      all[id] = placeholder ? ("ph:" + String(url).split("/").pop()) : url;
      const keys = Object.keys(all);
      if (keys.length > 2500) keys.slice(0, keys.length - 2000).forEach((k) => delete all[k]);
      localStorage.setItem(coverLsKey, JSON.stringify(all));
    } catch (_) {}
  }
  let coverObs = null;
  const coverQueue = [];
  const coverAsked = new Set();
  let coverActive = 0;
  const COVER_MAX = 4;
  function enqueueCover(img, id) {
    if (!id || coverAsked.has(id) || coverInFlight.has(id) || coverCache.has(id) || coverLsGet(id)) return;
    if (coverQueue.some((j) => j.id === id)) return;
    coverQueue.push({ img, id });
    drainCoverQueue();
  }
  function observeCover(img) {
    if (state.coverScrollLock) return;
    const id = img && img.dataset.need;
    if (!id || coverAsked.has(id) || coverLsGet(id)) return;
    if (!coverObs) {
      const root = $("playlistView");
      coverObs = new IntersectionObserver((ents) => {
        ents.forEach((en) => {
          if (!en.isIntersecting) return;
          const nid = en.target.dataset.need;
          if (nid) enqueueCover(en.target, nid);
          coverObs.unobserve(en.target);
        });
      }, { root: root, rootMargin: "16px", threshold: 0.01 });
    }
    coverObs.observe(img);
  }
  function drainCoverQueue() {
    while (coverQueue.length && coverActive < COVER_MAX) {
      const job = coverQueue.shift();
      if (!job || !job.id || coverAsked.has(job.id)) continue;
      coverAsked.add(job.id);
      coverActive++;
      const { img, id } = job;
      (async () => {
        try {
          const t = state.tracks.find((x) => x.id === id);
          if (t) await loadCover(t);
          const url = coverLsGet(id);
          if (url && img && img.dataset.need === id) img.src = url;
        } finally {
          await new Promise((r) => setTimeout(r, 40));
          coverActive--;
          if (coverQueue.length) drainCoverQueue();
        }
      })();
    }
  }

  const audio = $("audio");
  const seek = $("seek");
  const vol = $("vol");
  $("coverImg").addEventListener("error", () => {
    $("coverImg").style.visibility = "hidden";
  });
  $("coverImg").addEventListener("load", () => {
    $("coverImg").style.visibility = "visible";
  });

  // ---------- утилиты ----------
  function fmt(sec) {
    if (!sec || !isFinite(sec) || sec < 0) return "0:00";
    sec = Math.floor(sec);
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${s.toString().padStart(2, "0")}`;
  }

  function toast(msg) {
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), 3800);
  }

  function setOllamaPill(info) {
    const pill = $("ollamaPill");
    const lbl = pill.querySelector(".lbl");
    pill.classList.remove("ok", "warn", "err");
    if (!info) {
      lbl.textContent = "Ollama?";
      return;
    }
    if (info.online) {
      const missing = Object.entries(info.models || {})
        .filter(([, v]) => !v)
        .map(([k]) => k);
      if (missing.length) {
        pill.classList.add("warn");
        lbl.textContent = "Ollama · нет " + missing[0];
      } else {
        pill.classList.add("ok");
        lbl.textContent = "Ollama · ИИ";
      }
    } else {
      pill.classList.add("err");
      lbl.textContent = "Эвристика";
    }
  }

  async function api(path, opts) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!r.ok) {
      let msg = r.statusText;
      try {
        const j = await r.json();
        msg = j.detail || JSON.stringify(j);
      } catch (_) {
        msg = await r.text();
      }
      throw new Error(msg || "Ошибка API");
    }
    return r.json();
  }

  // ---------- плейлист (виртуальный скролл) ----------
  function rowH() {
    const v = getComputedStyle(document.documentElement).getPropertyValue("--row");
    const n = parseFloat(v);
    const fs = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    return (n || 3.35) * fs;
  }

  function renderPlaylist() {
    const q = $("search").value.trim().toLowerCase();
    const src = [];
    for (let i = 0; i < state.tracks.length; i++) {
      const t = state.tracks[i];
      t._i = i;
      if (state.favoritesOnly && !state.favorites.has(t.id)) continue;
      if (state.artistFilter && !`${t.artist} ${t.title} ${t.album} ${t.filename}`.toLowerCase().includes(state.artistFilter.toLowerCase())) continue;
      if (!q) {
        src.push(t);
        continue;
      }
      const blob = `${t.artist} ${t.title} ${t.album} ${t.filename}`.toLowerCase();
      if (blob.includes(q)) src.push(t);
    }
    state.filtered = src;
    $("plCount").textContent = `${src.length} ${plural(src.length)}`;
    syncFavoriteControls();
    syncFavoritesFilter();
    virtPaint(true);
  }

  function virtPaint() {
    const view = $("playlistView");
    const spacer = $("playlistSpacer");
    const ul = $("playlist");
    if (!view || !ul) return;
    const rh = rowH();
    const n = state.filtered.length;
    spacer.style.height = `${n * rh}px`;
    const h = Math.min(view.clientHeight || 400, 900);
    const vis = Math.min(18, Math.max(8, Math.ceil(h / rh)));
    const start = Math.max(0, Math.floor(view.scrollTop / rh) - 4);
    const end = Math.min(n, start + Math.min(28, vis + 8));
    const count = Math.max(0, end - start);
    while (ul.children.length < count) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="idx"><span class="n"></span><span class="play-mini">▶</span></span><img class="row-cover" alt=""/><div><div class="t-title"></div><div class="t-artist"></div></div><div class="t-right"><span class="tag-slot"></span><span class="dur"></span></div>`;
      ul.appendChild(li);
    }
    while (ul.children.length > count) ul.lastChild.remove();
    for (let k = 0; k < count; k++) {
      const t = state.filtered[start + k];
      const i = t._i;
      const li = ul.children[k];
      li.dataset.id = t.id;
      li.dataset.i = String(i);
      li.classList.toggle("active", i === state.index);
      li.style.setProperty("--y", `${(start + k) * rh}px`);
      const nStr = String(i + 1).padStart(2, "0");
      const nEl = li.querySelector(".n");
      if (nEl.textContent !== nStr) nEl.textContent = nStr;
      const titleEl = li.querySelector(".t-title");
      if (titleEl.textContent !== t.title) titleEl.textContent = t.title;
      const artEl = li.querySelector(".t-artist");
      if (artEl.textContent !== t.artist) artEl.textContent = t.artist;
      const durStr = fmt(t.duration);
      const durEl = li.querySelector(".dur");
      if (durEl.textContent !== durStr) durEl.textContent = durStr;
      const tag = mediaTag(t);
      const slot = li.querySelector(".tag-slot");
      if (slot.dataset.v !== tag) {
        slot.dataset.v = tag;
        slot.innerHTML = tag;
      }
      const img = li.querySelector(".row-cover");
      const cached = coverLsGet(t.id);
      if (cached) {
        if (img.dataset.need !== "" || img.getAttribute("src") !== cached) {
          img.dataset.need = "";
          if (img.getAttribute("src") !== cached) img.src = cached;
        }
        img.style.opacity = "1";
      } else {
        img.dataset.need = t.id;
        if (!state.coverScrollLock) observeCover(img);
      }
    }
  }

  function scrollToActive() {
    const view = $("playlistView");
    if (!view || state.index < 0) return;
    const k = state.filtered.findIndex((t) => t._i === state.index);
    if (k < 0) return;
    const rh = rowH();
    const y = k * rh - view.clientHeight / 2 + rh;
    view.scrollTop = Math.max(0, y);
  }

  function plural(n) {
    const n10 = n % 10,
      n100 = n % 100;
    if (n10 === 1 && n100 !== 11) return "трек";
    if (n10 >= 2 && n10 <= 4 && (n100 < 10 || n100 >= 20)) return "трека";
    return "треков";
  }

  function mediaTag(t) {
    const d = t.media && t.media.display;
    if (d === "clip") return '<span class="tag clip">КЛИП</span>';
    if (d === "cover") return '<span class="tag cover">ОБЛОЖКА</span>';
    if (d === "placeholder") return '<span class="tag ph">КУРЫМДЫК</span>';
    return "";
  }

  $("playlist").addEventListener("click", (e) => {
    const art = e.target.closest(".t-artist");
    if (art) {
      e.stopPropagation();
      const li = art.closest("li");
      const t = li && state.tracks[Number(li.dataset.i)];
      if (t && t.artist) filterByArtist(t.artist);
      return;
    }
    const li = e.target.closest("li");
    if (!li) return;
    playIndex(Number(li.dataset.i));
  });

  $("search").addEventListener("input", () => {
    $("playlistView").scrollTop = 0;
    renderPlaylist();
  });
  $("playlistView").addEventListener(
    "scroll",
    () => {
      state.coverScrollLock = true;
      clearTimeout(state.coverScrollTimer);
      state.coverScrollTimer = setTimeout(() => {
        state.coverScrollLock = false;
        virtPaint();
      }, 150);
      if (state.virtRaf) return;
      state.virtRaf = requestAnimationFrame(() => {
        state.virtRaf = 0;
        virtPaint();
      });
    },
    { passive: true }
  );

  // ---------- YouTube IFrame API ----------
  function loadYouTubeAPI() {
    return new Promise((resolve) => {
      if (window.YT && window.YT.Player) return resolve(true);
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => {
        if (prev) prev();
        resolve(true);
      };
      if (!document.getElementById("yt-iframe-api")) {
        const s = document.createElement("script");
        s.id = "yt-iframe-api";
        s.src = "https://www.youtube.com/iframe_api";
        s.onerror = () => resolve(false);
        document.head.appendChild(s);
      }
      setTimeout(() => resolve(!!(window.YT && window.YT.Player)), 8000);
    });
  }

  function destroyYT() {
    try {
      if (state.yt && state.yt.destroy) state.yt.destroy();
    } catch (_) {}
    state.yt = null;
    $("ytPlayer").innerHTML = "";
  }

  async function playYouTube(videoId) {
    const ok = await loadYouTubeAPI();
    if (!ok || !window.YT) {
      toast("YouTube недоступен — играю локальный файл");
      return false;
    }
    $("videoWrap").classList.remove("hidden");
    $("coverWrap").classList.remove("hidden");
    $("mediaLayer").classList.add("is-clip");
    showClipLoader(true);
    requestAnimationFrame(() => $("videoWrap").classList.add("on"));
    destroyYT();
    // контейнер пересоздаём, YT.Player требует элемент
    const host = $("ytPlayer");
    host.innerHTML = "";
    const div = document.createElement("div");
    div.id = "ytPlayerInner";
    host.appendChild(div);

    await new Promise((resolve) => {
      state.yt = new YT.Player("ytPlayerInner", {
        videoId,
        playerVars: {
          autoplay: 1,
          rel: 0,
          modestbranding: 1,
          iv_load_policy: 3,
          playsinline: 1,
          origin: location.origin,
          fs: 0,
          cc_load_policy: 0,
          cc_lang_pref: "",
          hl: "ru",
          disablekb: 0,
          showinfo: 0,
        },
        events: {
          onReady: (e) => {
            state.ytReady = true;
            try {
              e.target.unloadModule("captions");
              e.target.unloadModule("cc");
              e.target.setOption("captions", "track", {});
            } catch (_) {}
            try {
              e.target.setVolume(state.muted ? 0 : state.volume * 100);
              e.target.playVideo();
            } catch (_) {}
            resolve();
          },
          onStateChange: (e) => {
            if (e.data === YT.PlayerState.ENDED) onEnded();
            if (e.data === YT.PlayerState.PLAYING) {
              try {
                e.target.unloadModule("captions");
                e.target.unloadModule("cc");
              } catch (_) {}
              showClipLoader(false);
              setPlaying(true);
            }
            if (e.data === YT.PlayerState.PAUSED) setPlaying(false);
          },
          onError: () => {
            showClipLoader(false);
            toast("Клип не встроился — играю аудиофайл");
            fallbackAudio();
          },
        },
      });
      setTimeout(resolve, 4000);
    });
    state.mode = "youtube";
    audio.pause();
    return true;
  }

  function showClipLoader(on) {
    const el = $("clipLoader");
    if (!el) return;
    el.classList.toggle("hidden", !on);
    if (on) {
      const src = ($("coverBlur") && $("coverBlur").style.backgroundImage) || "";
      const blur = $("clipLoaderBlur");
      if (blur) blur.style.backgroundImage = src;
    }
  }

  function fallbackAudio() {
    showClipLoader(false);
    $("videoWrap").classList.add("hidden");
    $("videoWrap").classList.remove("on");
    $("coverWrap").classList.remove("hidden");
    $("mediaLayer").classList.remove("is-clip");
    state.mode = "audio";
    destroyYT();
    audio.play().catch(() => {});
  }

  // ---------- воспроизведение ----------
  function setPlaying(v) {
    if (state.playing && !v) {
      samplePlaySession();
      finishPlaySession();
    }
    state.playing = v;
    if (v) ensurePlaySession();
    $("iconPlay").classList.toggle("hidden", v);
    $("iconPause").classList.toggle("hidden", !v);
    const vinyl = $("vinyl");
    if (vinyl) vinyl.classList.toggle("spin", v);
    const fsPlay = $("fsPlay");
    if (fsPlay) fsPlay.textContent = v ? "⏸" : "▶";
    $("coverWrap").classList.toggle("is-clip", state.mode === "youtube");
    if (v && currentTrack()) buildWave(currentTrack().id);
    updateNowPlaying();
  }

  function showWelcome(on) {
    $("welcome").classList.toggle("hidden", !on);
    $("mediaLayer").classList.toggle("hidden", on);
  }

  async function playIndex(i, fromWave) {
    if (i < 0 || i >= state.tracks.length) return;
    if (state.waveMode && !fromWave) {
      sendWaveSignal("skip");
      setWaveMode(false);
    }
    if (state.playSession && state.playSession.trackId !== state.tracks[i].id) {
      samplePlaySession();
      finishPlaySession();
    }
    const gen = ++state.playGen;
    state.index = i;
    const t = state.tracks[i];
    renderPlaylist();
    scrollToActive();

    showWelcome(false);
    $("nowTitle").textContent = t.title;
    $("nowArtist").textContent = t.artist;
    const fsTitle = $("fsTitle");
    const fsArtist = $("fsArtist");
    if (fsTitle) fsTitle.textContent = t.title;
    if (fsArtist) fsArtist.textContent = t.artist;
    $("nowAlbum").textContent = [t.album, t.year].filter(Boolean).join(" · ");
    $("waveReason").textContent = fromWave ? state.waveReason : "";
    $("nowReason").textContent = "";
    document.title = `${t.artist} — ${t.title} · Курымдык`;
    updateNowPlaying();

    $("videoWrap").classList.add("hidden");
    $("videoWrap").classList.remove("on");
    $("coverWrap").classList.remove("hidden");
    $("mediaLayer").classList.remove("is-clip");
    destroyYT();
    state.mode = "audio";

    audio.src = `/api/stream/${t.id}`;
    audio.play().catch(() => {});
    setPlaying(true);

    if (t.has_embedded_cover && t.embedded_cover) {
      const name = String(t.embedded_cover).split(/[/\\]/).pop();
      if (name) setCover(`/api/cover/${encodeURIComponent(name)}`);
    }

    loadCover(t, gen);
    loadLyrics(t, gen);
    loadClip(t, gen);
    prefetchCovers(i);
  }

  async function loadCover(t, gen) {
    if (coverCache.has(t.id)) {
      applyCover(coverCache.get(t.id), gen);
      return;
    }
    const ls = coverLsGet(t.id);
    if (ls) {
      coverAsked.add(t.id);
      if (gen !== undefined && gen === state.playGen) setCover(ls);
      return;
    }
    if (coverInFlight.has(t.id)) return;
    coverAsked.add(t.id);
    coverInFlight.add(t.id);
    try {
      const data = await api(`/api/cover-find/${t.id}`);
      coverCache.set(t.id, data);
      t.cover = data;
      if (data && data.cover_file) {
        coverLsSet(t.id, coverUrlOf(data), !!data.placeholder);
      }
      applyCover(data, gen);
    } catch (_) {
      coverAsked.delete(t.id);
    } finally {
      coverInFlight.delete(t.id);
    }
  }

  function applyCover(data, gen) {
    if (!data || !data.cover_file) return;
    if (gen !== undefined && gen !== state.playGen) return;
    const cur = currentTrack();
    if (gen === undefined && (!cur || (data.track_id && cur.id !== data.track_id))) return;
    const badge = $("modeBadge");
    if (state.mode !== "youtube") {
      badge.className = "badge " + (data.placeholder ? "ph" : "cover");
      badge.textContent = data.placeholder ? "ПЛЕЙСХОЛДЕР" : "ОБЛОЖКА";
    }
    const url = coverUrlOf(data);
    coverLsSet(data.track_id || (cur && cur.id), url, !!data.placeholder);
    setCover(url);
  }

  async function loadClip(t, gen) {
    if (clipCache.has(t.id)) {
      applyClip(t, clipCache.get(t.id), gen);
      return;
    }
    if (resolvingInFlight.has(t.id)) return;
    resolvingInFlight.add(t.id);
    $("resolving").classList.remove("hidden");
    $("resolvingText").textContent = "Ищем клип…";
    try {
      const media = await api(`/api/resolve/${t.id}`, { method: "POST" });
      clipCache.set(t.id, media);
      t.media = media;
      applyClip(t, media, gen);
      renderPlaylist();
    } catch (err) {
      if (gen === state.playGen) toast("Клип: " + err.message);
    } finally {
      resolvingInFlight.delete(t.id);
      if (gen === state.playGen) $("resolving").classList.add("hidden");
    }
  }

  function applyClip(t, media, gen) {
    if (gen !== undefined && gen !== state.playGen) return;
    if (!media) return;
    $("nowReason").textContent = media.reason || "";
    if (media.display === "clip" && media.youtube_id) {
      const badge = $("modeBadge");
      badge.className = "badge clip";
      badge.textContent = "КЛИП";
      playYouTube(media.youtube_id);
      if (media.thumb) {
        setCover(`/api/cover-proxy?url=${encodeURIComponent(media.thumb)}`);
      }
    }
  }

  function setCover(url) {
    if (!url) return;
    if (/^https?:\/\//i.test(url) && url.indexOf(location.origin) !== 0) {
      url = `/api/cover-proxy?url=${encodeURIComponent(url)}`;
    }
    const a = $("coverImg");
    const b = $("coverImgB");
    const prev = a.src;
    if (b && prev && prev !== url && !prev.endsWith("/")) {
      b.src = prev;
      b.classList.add("show");
      b.style.opacity = "1";
      b.style.filter = "blur(0)";
      requestAnimationFrame(() => {
        b.style.opacity = "0";
        b.style.filter = "blur(8px)";
        b.style.transform = "scale(1.05)";
        setTimeout(() => b.classList.remove("show"), 420);
      });
    }
    a.style.opacity = "0";
    a.style.filter = "blur(8px)";
    a.style.transform = "scale(1.05)";
    a.src = url;
    $("coverBlur").style.backgroundImage = `url("${url}")`;
    const bg = $("bgCover");
    if (bg) bg.style.backgroundImage = `url("${url}")`;
    a.onload = () => {
      a.style.visibility = "visible";
      a.style.transition = "opacity 0.4s ease, filter 0.4s ease, transform 0.4s ease";
      a.style.opacity = "1";
      a.style.filter = "blur(0)";
      a.style.transform = "scale(1)";
      a.classList.toggle("lo-res", a.naturalWidth > 0 && a.naturalWidth < 800);
      paintFromCover(a);
    };
  }

  function prefetchCovers(i) {
    [i + 1, i - 1].forEach((j) => {
      const t = state.tracks[j];
      if (!t || coverCache.has(t.id) || coverAsked.has(t.id) || coverLsGet(t.id)) return;
      enqueueCover(null, t.id);
    });
  }

  function currentTrack() {
    return state.tracks[state.index] || null;
  }

  function newSessionId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function ensurePlaySession() {
    const track = currentTrack();
    if (!track || (state.playSession && state.playSession.trackId === track.id)) return;
    state.playSession = {
      trackId: track.id,
      sessionId: newSessionId(),
      duration: 0,
      lastPosition: getPosition(),
      lastWall: performance.now(),
      persisted: false,
    };
  }

  function samplePlaySession() {
    const session = state.playSession;
    if (!session || !state.playing) return;
    const now = performance.now();
    const position = getPosition();
    const wallDelta = Math.max(0, (now - session.lastWall) / 1000);
    const positionDelta = position - session.lastPosition;
    if (positionDelta > 0 && positionDelta <= wallDelta + 2.5) {
      session.duration += Math.min(positionDelta, wallDelta + 0.25);
    }
    session.lastPosition = position;
    session.lastWall = now;
    if (session.duration > 30 && !session.persisted) {
      session.persisted = true;
      persistPlaySession(session);
    }
  }

  async function persistPlaySession(session) {
    const track = state.tracks.find((item) => item.id === session.trackId);
    if (!track || session.duration <= 30) return;
    try {
      await api("/api/stats/play", {
        method: "POST",
        body: JSON.stringify({
          track_id: session.trackId,
          session_id: session.sessionId,
          duration: Math.floor(session.duration * 10) / 10,
        }),
      });
    } catch (error) {
      console.warn("Listening statistics could not be saved:", error.message);
    }
  }

  function finishPlaySession() {
    const session = state.playSession;
    if (!session) return;
    if (session.duration > 30) persistPlaySession(session);
    state.playSession = null;
  }

  function syncFavoriteControls() {
    const track = currentTrack();
    const favorite = !!track && state.favorites.has(track.id);
    const button = $("btnFavorite");
    if (button) {
      button.disabled = !track;
      button.classList.toggle("on", favorite);
      button.setAttribute("aria-pressed", String(favorite));
      button.title = favorite ? "Убрать из избранного" : "В избранное";
      button.setAttribute("aria-label", button.title);
    }
    const dislike = $("btnDislike");
    if (dislike) dislike.disabled = !track;
    const filter = $("btnFavoritesFilter");
    if (filter) {
      filter.classList.toggle("on", state.favoritesOnly);
      filter.setAttribute("aria-pressed", String(state.favoritesOnly));
      filter.title = state.favoritesOnly ? "Показать все треки" : "Показать избранное";
      filter.setAttribute("aria-label", filter.title);
    }
  }

  function toggleCurrentFavorite() {
    const track = currentTrack();
    if (!track) return;
    if (state.favorites.has(track.id)) state.favorites.delete(track.id);
    else state.favorites.add(track.id);
    if (state.favorites.has(track.id)) sendWaveSignal("like");
    try { localStorage.setItem(favoritesKey, JSON.stringify([...state.favorites])); } catch (_) {}
    if (state.favoritesOnly && !state.favorites.has(track.id)) {
      $("playlistView").scrollTop = 0;
    }
    renderPlaylist();
  }

  function toggleFavoritesFilter() {
    state.favoritesOnly = !state.favoritesOnly;
    $("playlistView").scrollTop = 0;
    renderPlaylist();
  }

  function setWaveMode(enabled) {
    state.waveMode = enabled;
    const button = $("btnWave");
    if (button) {
      button.classList.toggle("on", enabled);
      button.setAttribute("aria-pressed", String(enabled));
      button.textContent = enabled ? "Волна · вкл." : "Моя волна";
    }
    if (!enabled) {
      state.waveQueue = [];
      state.waveReason = "";
      $("waveReason").textContent = "";
    }
  }

  async function fetchWaveQueue() {
    const recent = [...state.waveSeen].slice(-30);
    const current = currentTrack();
    if (current && !recent.includes(current.id)) recent.push(current.id);
    const query = new URLSearchParams({ count: "10", exclude: recent.join(",") });
    const response = await api(`/api/wave/queue?${query.toString()}`);
    state.waveQueue.push(...(response.items || []));
  }

  async function playWaveNext() {
    if (!state.waveMode || state.waveLoading) return;
    state.waveLoading = true;
    try {
      if (!state.waveQueue.length) await fetchWaveQueue();
      if (!state.waveQueue.length && state.waveSeen.size >= state.tracks.length) {
        state.waveSeen.clear();
        const current = currentTrack();
        if (current) state.waveSeen.add(current.id);
        await fetchWaveQueue();
      }
      const item = state.waveQueue.shift();
      if (!item) {
        setWaveMode(false);
        toast("Не удалось подобрать следующий трек для волны");
        return;
      }
      const index = state.tracks.findIndex((track) => track.id === item.id);
      if (index < 0) {
        toast("Рекомендованный трек больше не в библиотеке");
        return;
      }
      state.waveSeen.add(item.id);
      state.waveReason = item.reason || "подборка по твоим вкусам";
      playIndex(index, true);
    } catch (error) {
      setWaveMode(false);
      toast(`Моя волна: ${error.message}`);
    } finally {
      state.waveLoading = false;
    }
  }

  async function startWave() {
    if (state.waveMode) {
      setWaveMode(false);
      return;
    }
    if (!state.tracks.length) {
      toast("Сначала выбери папку с музыкой");
      return;
    }
    state.waveSeen.clear();
    state.waveQueue = [];
    const current = currentTrack();
    if (current) state.waveSeen.add(current.id);
    setWaveMode(true);
    await playWaveNext();
  }

  function sendWaveSignal(signal) {
    const track = currentTrack();
    if (!track) return;
    const duration = getPosition();
    if (signal === "skip" && duration >= 30) return;
    api("/api/wave/signal", {
      method: "POST",
      body: JSON.stringify({ track_id: track.id, signal, duration }),
    }).catch((error) => console.warn("Wave feedback was not saved:", error.message));
  }

  function onEnded() {
    if (state.repeat === "one") {
      if (state.mode === "youtube" && state.yt) {
        state.yt.seekTo(0);
        state.yt.playVideo();
      } else {
        audio.currentTime = 0;
        audio.play();
      }
      return;
    }
    if (state.waveMode) {
      sendWaveSignal("complete");
      playWaveNext();
      return;
    }
    next(true);
  }

  function next(fromEnded) {
    if (state.waveMode) {
      if (!fromEnded) sendWaveSignal("skip");
      playWaveNext();
      return;
    }
    if (!state.tracks.length) return;
    let i;
    if (state.shuffle) {
      if (state.tracks.length === 1) i = 0;
      else {
        do {
          i = Math.floor(Math.random() * state.tracks.length);
        } while (i === state.index && state.tracks.length > 1);
      }
    } else {
      i = state.index + 1;
      if (i >= state.tracks.length) {
        if (state.repeat === "all" || !fromEnded) i = 0;
        else {
          setPlaying(false);
          return;
        }
      }
    }
    playIndex(i);
  }

  function prev() {
    if (state.waveMode) {
      sendWaveSignal("skip");
      playWaveNext();
      return;
    }
    if (!state.tracks.length) return;
    const pos = getPosition();
    if (pos > 3) {
      seekTo(0);
      return;
    }
    const i = state.index <= 0 ? state.tracks.length - 1 : state.index - 1;
    playIndex(i);
  }

  function togglePlay() {
    if (state.index < 0 && state.tracks.length) {
      playIndex(0);
      return;
    }
    if (state.mode === "youtube" && state.yt) {
      const st = state.yt.getPlayerState && state.yt.getPlayerState();
      if (st === 1) state.yt.pauseVideo();
      else state.yt.playVideo();
      return;
    }
    if (audio.paused) audio.play().catch(() => {});
    else audio.pause();
  }

  function getDuration() {
    if (state.mode === "youtube" && state.yt && state.yt.getDuration) {
      try {
        return state.yt.getDuration() || 0;
      } catch (_) {}
    }
    return audio.duration || (currentTrack() && currentTrack().duration) || 0;
  }

  function getPosition() {
    if (state.mode === "youtube" && state.yt && state.yt.getCurrentTime) {
      try {
        return state.yt.getCurrentTime() || 0;
      } catch (_) {}
    }
    return audio.currentTime || 0;
  }

  function seekTo(sec) {
    if (state.mode === "youtube" && state.yt && state.yt.seekTo) {
      state.yt.seekTo(sec, true);
    } else {
      audio.currentTime = sec;
    }
  }

  function applyVolume() {
    const v = state.muted ? 0 : state.volume;
    audio.volume = v;
    if (state.yt && state.yt.setVolume) {
      try {
        state.yt.setVolume(v * 100);
        if (state.muted) state.yt.mute();
        else state.yt.unMute();
      } catch (_) {}
    }
  }

  audio.addEventListener("play", () => setPlaying(true));
  audio.addEventListener("pause", () => {
    if (state.mode === "audio") setPlaying(false);
  });
  audio.addEventListener("ended", () => {
    if (state.mode === "audio") onEnded();
  });

  seek.addEventListener("input", () => {
    state.seeking = true;
  });
  seek.addEventListener("change", () => {
    const d = getDuration();
    seekTo((Number(seek.value) / 1000) * d);
    state.seeking = false;
  });

  vol.addEventListener("input", () => {
    state.volume = Number(vol.value) / 100;
    state.muted = state.volume === 0;
    applyVolume();
  });

  function tick() {
    const d = getDuration();
    const p = getPosition();
    samplePlaySession();
    if (!state.seeking && d) {
      const v = String(Math.round((p / d) * 1000));
      seek.value = v;
      const fsSeek = $("fsSeek");
      if (fsSeek && document.activeElement !== fsSeek) fsSeek.value = v;
    }
    $("curTime").textContent = fmt(p);
    $("durTime").textContent = fmt(d);
    syncLyrics(p);
    drawViz();
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  // ---------- визуализатор спектра (только локальное аудио) ----------
  let analyser = null;
  let freq = null;
  let audioCtx = null;

  function ensureAnalyser() {
    if (analyser) {
      if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
      return;
    }
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const src = audioCtx.createMediaElementSource(audio);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 128;
      analyser.smoothingTimeConstant = 0.72;
      src.connect(analyser);
      analyser.connect(audioCtx.destination);
      freq = new Uint8Array(analyser.frequencyBinCount);
    } catch (_) {}
  }

  let lastViz = 0;
  const sparks = [];
  function drawViz() {
    const now = performance.now();
    if (now - lastViz < 33) return;
    lastViz = now;
    const wrap = $("coverWrap");
    if (state.mode !== "audio" || !state.playing) {
      state.beat += (0 - state.beat) * 0.2;
      const z = state.beat.toFixed(3);
      if (wrap) {
        wrap.style.setProperty("--beat", z);
        wrap.style.setProperty("--mids", "0");
      }
      document.documentElement.style.setProperty("--beat", z);
      document.documentElement.style.setProperty("--mids", "0");
      const eq = $("eqBars");
      if (eq) {
        const ctx = eq.getContext("2d");
        ctx.clearRect(0, 0, eq.width, eq.height);
        const acc = getComputedStyle(document.documentElement).getPropertyValue("--accent-1").trim() || "#fff";
        const bars = 56, gap = 2, bw = (eq.width - gap * bars) / bars, h = eq.height;
        for (let i = 0; i < bars; i++) {
          const bh = 3 + state.beat * 8;
          ctx.fillStyle = acc;
          ctx.globalAlpha = 0.2;
          ctx.fillRect(i * (bw + gap), h - bh, Math.max(1, bw), bh);
        }
        ctx.globalAlpha = 1;
      }
      return;
    }
    ensureAnalyser();
    if (!analyser || !freq) return;
    analyser.fftSize = 256;
    analyser.getByteFrequencyData(freq);
    let bass = 0, mids = 0, highs = 0;
    for (let i = 0; i < 10; i++) bass += freq[i] || 0;
    for (let i = 10; i < 30; i++) mids += freq[i] || 0;
    for (let i = 30; i < 60; i++) highs += freq[i] || 0;
    bass = bass / (10 * 255);
    mids = mids / (20 * 255);
    highs = highs / (30 * 255);
    state.beat += (bass - state.beat) * 0.32;
    const b = state.beat.toFixed(3);
    const m = mids.toFixed(3);
    if (wrap) {
      wrap.style.setProperty("--beat", b);
      wrap.style.setProperty("--mids", m);
    }
    document.documentElement.style.setProperty("--beat", b);
    document.documentElement.style.setProperty("--mids", m);

    const glow =
      getComputedStyle(document.documentElement).getPropertyValue("--glow").trim() ||
      "220, 216, 208";
    const eq = $("eqBars");
    if (eq) {
      const ctx = eq.getContext("2d");
      const w = eq.width, h = eq.height;
      ctx.clearRect(0, 0, w, h);
      const bars = 56;
      const gap = 2;
      const bw = (w - gap * bars) / bars;
      const acc = getComputedStyle(document.documentElement).getPropertyValue("--accent-1").trim() || "#fff";
      for (let i = 0; i < bars; i++) {
        const idx = Math.floor((i / bars) * (freq.length - 1));
        let v = (freq[idx] || 0) / 255;
        if (!state.playing) v *= 0.15;
        const bh = Math.max(3, v * h);
        const g = ctx.createLinearGradient(0, h - bh, 0, h);
        g.addColorStop(0, "#fff");
        g.addColorStop(1, acc);
        ctx.fillStyle = g;
        ctx.globalAlpha = 0.35 + v * 0.65;
        ctx.fillRect(i * (bw + gap), h - bh, Math.max(1, bw), bh);
      }
      ctx.globalAlpha = 1;
    }
    const canvas = $("viz");
    if (canvas) {
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      const bars = 32;
      const gap = 3;
      const bw = (w - gap * bars) / bars;
      for (let i = 0; i < bars; i++) {
        const v = freq[Math.min(freq.length - 1, i)] / 255;
        const bh = Math.max(2, v * h);
        ctx.fillStyle = `rgba(${glow}, ${0.18 + v * 0.7})`;
        ctx.fillRect(i * (bw + gap), h - bh, bw, bh);
      }
    }

    const sp = $("sparks");
    if (sp && highs > 0.22) {
      const sctx = sp.getContext("2d");
      if (sp.width !== 640) {
        sp.width = 640;
        sp.height = 640;
      }
      sctx.clearRect(0, 0, 640, 640);
      if (highs > 0.38 && sparks.length < 28) {
        sparks.push({
          x: Math.random() * 640,
          y: Math.random() < 0.5 ? Math.random() * 50 : 590 + Math.random() * 50,
          vx: (Math.random() - 0.5) * 4,
          vy: (Math.random() - 0.5) * 4,
          life: 1,
        });
      }
      const glow =
        getComputedStyle(document.documentElement).getPropertyValue("--glow").trim() ||
        "220, 216, 208";
      for (let i = sparks.length - 1; i >= 0; i--) {
        const p = sparks[i];
        p.x += p.vx;
        p.y += p.vy;
        p.life -= 0.045;
        if (p.life <= 0) {
          sparks.splice(i, 1);
          continue;
        }
        sctx.fillStyle = `rgba(${glow}, ${p.life})`;
        sctx.beginPath();
        sctx.arc(p.x, p.y, 1.6, 0, Math.PI * 2);
        sctx.fill();
      }
    } else if (sp) {
      const sctx = sp.getContext("2d");
      sctx && sctx.clearRect(0, 0, sp.width, sp.height);
      sparks.length = 0;
    }
    if (state.beat > 0.55) {
      const v = $("vinyl");
      if (v) {
        const r = v.getBoundingClientRect();
        burstBass(r.left + r.width / 2, r.top + r.height / 2);
      }
    }
    const ct = currentTrack();
    if (ct && waveCache[ct.id] && waveCache[ct.id] !== true) drawWave(waveCache[ct.id]);
  }

  // ---------- кнопки и горячие клавиши ----------
  $("btnPlay").addEventListener("click", togglePlay);
  $("btnFavorite").addEventListener("click", toggleCurrentFavorite);
  $("btnDislike")?.addEventListener("click", () => {
    const track = currentTrack();
    if (!track) return;
    sendWaveSignal("dislike");
    toast("Учту: меньше таких треков");
  });
  $("btnWave")?.addEventListener("click", startWave);
  $("btnWaveHero")?.addEventListener("click", startWave);
  $("btnFavoritesFilter").addEventListener("click", toggleFavoritesFilter);
  $("btnNext").addEventListener("click", () => next(false));
  $("btnPrev").addEventListener("click", prev);
  $("btnShuffle").addEventListener("click", () => {
    state.shuffle = !state.shuffle;
    $("btnShuffle").classList.toggle("on", state.shuffle);
  });
  $("btnRepeat").addEventListener("click", () => {
    state.repeat = state.repeat === "off" ? "all" : state.repeat === "all" ? "one" : "off";
    $("btnRepeat").classList.toggle("on", state.repeat !== "off");
    $("btnRepeat").title =
      state.repeat === "one" ? "Повтор трека" : state.repeat === "all" ? "Повтор плейлиста" : "Повтор выключен";
  });
  $("btnMute").addEventListener("click", () => {
    state.muted = !state.muted;
    applyVolume();
  });

  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (e.code === "Space") {
      e.preventDefault();
      togglePlay();
    } else if (e.code === "ArrowLeft") {
      e.preventDefault();
      prev();
    } else if (e.code === "ArrowRight") {
      e.preventDefault();
      next(false);
    } else if (e.key === "m" || e.key === "M" || e.key === "ь") {
      state.muted = !state.muted;
      applyVolume();
    } else if (e.key === "f" || e.key === "F" || e.key === "а") {
      e.preventDefault();
      toggleFullscreen();
    } else if (e.code === "Escape" && (document.fullscreenElement || document.webkitFullscreenElement)) {
      toggleFullscreen(false);
    }
  });

  if ("mediaSession" in navigator) {
    navigator.mediaSession.setActionHandler("play", togglePlay);
    navigator.mediaSession.setActionHandler("pause", togglePlay);
    navigator.mediaSession.setActionHandler("previoustrack", prev);
    navigator.mediaSession.setActionHandler("nexttrack", () => next(false));
  }

  // ---------- библиотека / папка ----------
  async function loadHealth(quiet) {
    try {
      const h = await api("/api/health");
      setOllamaPill(h.ollama);
      if (h.ollama && !h.ollama.online && !state._ollamaWarned) {
        state._ollamaWarned = true;
        toast("Ollama не запущена — клипы будут выбираться эвристикой (низкая точность)");
      }
      if (!quiet && !state.tracks.length) {
        if (h.folder && h.tracks) {
          const lib = await api("/api/library");
          applyLibrary(lib);
        } else {
          let path = "";
          try { path = localStorage.getItem(libraryPathKey) || ""; } catch (_) {}
          if (!path) {
            try {
              const roots = await api("/api/fs/roots");
              const music = (roots.roots || []).find((root) =>
                ["music", "музыка"].includes(String(root.name).toLowerCase())
              );
              path = music ? music.path : "";
            } catch (_) {}
          }
          if (path) {
            try {
              const lib = await api("/api/scan", {
                method: "POST",
                body: JSON.stringify({ path }),
              });
              applyLibrary(lib);
            } catch (_) {
              toast("Папка с музыкой недоступна. Выберите её снова.");
            }
          }
        }
      }
    } catch (_) {
      setOllamaPill({ online: false });
    }
  }

  function applyLibrary(lib) {
    state.folder = lib.folder;
    state.tracks = lib.tracks || [];
    state.artistsLoaded = false;
    state.artists = [];
    if (lib.folder) {
      try { localStorage.setItem(libraryPathKey, lib.folder); } catch (_) {}
    }
    $("folderChip").innerHTML = `<span>${esc(lib.folder || "Папка не выбрана")}</span>`;
    renderPlaylist();
    if (state.tracks.length) {
      $("welcome").querySelector("h1").textContent =
        `${state.tracks.length} ${plural(state.tracks.length)} в библиотеке`;
      $("welcome").querySelector("p").textContent =
        "Нажмите трек слева — Курымдык найдёт клип или обложку.";
    }
  }

  async function openFolder(path) {
    $("fsOpen").disabled = true;
    try {
      const lib = await api("/api/scan", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      applyLibrary(lib);
      closeModal();
      toast(`Загружено: ${lib.count} ${plural(lib.count)}`);
      showWelcome(true);
    } catch (err) {
      toast(err.message);
    } finally {
      $("fsOpen").disabled = false;
    }
  }

  $("btnRefresh").addEventListener("click", async () => {
    if (!state.tracks.length) {
      toast("Сначала выберите папку");
      return;
    }
    try {
      const r = await api("/api/resolve-all?force=true", { method: "POST" });
      toast(r.started ? "Обновляю клипы и обложки…" : r.message);
    } catch (err) {
      toast(err.message);
    }
  });

  const qualityCandidates = new Map();
  let qualityItems = [];
  let qualityPollTimer = 0;
  let qualityScanComplete = false;
  let qualityScanStatus = "idle";
  let qualitySuspectCount = 0;

  function qualityReason(reason) {
    return {
      short: "короче оригинала",
      keyword: "маркер версии",
      lowbitrate: "низкий битрейт",
    }[reason] || reason;
  }

  function renderQuality(items) {
    qualityItems = items || [];
    $("qualityReplaceAll").disabled = !qualityItems.length;
    if (!qualityItems.length) {
      const emptyMessage = qualityScanComplete
        ? "Подозрительных треков не найдено."
        : qualityScanStatus === "running"
          ? `Список появится после сканирования. Уже найдено: ${qualitySuspectCount}.`
          : "Сначала выполните сканирование.";
      $("qualityRows").innerHTML =
        `<tr><td colspan="5" class="muted">${esc(emptyMessage)}</td></tr>`;
      return;
    }
    $("qualityRows").innerHTML = qualityItems.map((item) => {
      const candidates = qualityCandidates.get(item.id) || item.candidates || [];
      const choices = candidates.map((candidate, index) =>
        `<option value="${index}">${esc(candidate.title)} · ${esc(candidate.channel)}</option>`
      ).join("");
      const candidateCell = candidates.length
        ? `<div class="quality-candidate"><select data-quality-choice="${esc(item.id)}">${choices}</select>` +
          `<small>${esc(candidates[0].duration)} сек. · <a href="${esc(candidates[0].url)}" target="_blank" rel="noopener noreferrer">Открыть</a></small></div>`
        : '<span class="muted">Не искали</span>';
      const reasons = (item.reasons || [item.reason]).map(qualityReason).join(", ");
      const duration = item.local_duration == null
        ? "локальная неизвестна"
        : `${esc(item.local_duration)} сек. / ${item.canonical_duration == null ? "оригинал неизвестен" : `${esc(item.canonical_duration)} сек.`}`;
      return `<tr data-quality-row="${esc(item.id)}">` +
        `<td class="quality-track">${esc(item.artist)} — ${esc(item.title)}<small>${esc(item.path)}</small></td>` +
        `<td>${esc(reasons)}</td><td>${duration}</td>` +
        `<td class="quality-candidate">${candidateCell}<button class="btn ghost" type="button" data-quality-search="${esc(item.id)}">Найти кандидатов</button></td>` +
        `<td><button class="btn primary" type="button" data-quality-replace="${esc(item.id)}" ${candidates.length ? "" : "disabled"}>Заменить</button></td>` +
        `</tr>`;
    }).join("");
  }

  function showQualityProgress(value, text, indeterminate) {
    const wrap = $("qualityProgressWrap");
    wrap.classList.toggle("hidden", !text);
    $("qualityProgressText").textContent = text || "";
    const progress = $("qualityProgress");
    progress.removeAttribute("indeterminate");
    if (indeterminate) progress.removeAttribute("value");
    else progress.value = value || 0;
  }

  async function pollQualityScan() {
    try {
      const status = await api("/api/uncensored/status");
      qualityScanStatus = status.status;
      qualitySuspectCount = status.suspects || 0;
      const percent = status.total ? Math.round(status.processed * 100 / status.total) : 0;
      if (status.status === "running") {
        $("qualitySummary").textContent = `Проверено ${status.processed} из ${status.total}; подозрительных: ${status.suspects}. MusicBrainz ограничивает частоту запросов.`;
        showQualityProgress(percent, `${status.processed}/${status.total}`, false);
        qualityPollTimer = setTimeout(pollQualityScan, 1200);
      } else if (status.status === "complete") {
        qualityScanComplete = true;
        showQualityProgress(100, `Готово: ${status.suspects} подозрительных`, false);
        $("qualitySummary").textContent = `Проверено ${status.total} треков. По причинам: ${Object.entries(status.by_reason || {}).map(([key, count]) => `${qualityReason(key)} — ${count}`).join("; ") || "нет совпадений"}.`;
        renderQuality(status.items);
      } else if (status.status === "error") {
        showQualityProgress(0, "Сканирование завершилось с ошибкой", false);
        $("qualitySummary").textContent = status.error || "Не удалось просканировать библиотеку.";
      }
    } catch (err) {
      showQualityProgress(0, err.message, false);
    }
  }

  $("btnQuality").addEventListener("click", async () => {
    if (!state.tracks.length) {
      toast("Сначала выберите папку");
      return;
    }
    $("qualityModal").classList.remove("hidden");
    clearTimeout(qualityPollTimer);
    try {
      const status = await api("/api/uncensored/status");
      qualityScanStatus = status.status;
      qualitySuspectCount = status.suspects || 0;
      qualityScanComplete = status.status === "complete";
      renderQuality(status.items || []);
      if (status.status === "running") {
        pollQualityScan();
      } else if (status.status === "complete") {
        await pollQualityScan();
      } else {
        $("qualitySummary").textContent = "Сканирование проверит теги, длительность и битрейт.";
        showQualityProgress(0, "", false);
      }
    } catch (err) {
      $("qualitySummary").textContent = err.message;
    }
  });

  $("qualityClose").addEventListener("click", () => $("qualityModal").classList.add("hidden"));
  $("qualityModal").addEventListener("click", (event) => {
    if (event.target.id === "qualityModal") $("qualityModal").classList.add("hidden");
  });

  $("qualityScan").addEventListener("click", async () => {
    $("qualityScan").disabled = true;
    qualityCandidates.clear();
    qualityScanComplete = false;
    qualityScanStatus = "running";
    qualitySuspectCount = 0;
    try {
      const status = await api("/api/uncensored/scan");
      $("qualitySummary").textContent = `Сканирование запущено: ${status.total} треков.`;
      showQualityProgress(0, `0/${status.total}`, false);
      renderQuality([]);
      pollQualityScan();
    } catch (err) {
      toast(err.message);
    } finally {
      $("qualityScan").disabled = false;
    }
  });

  $("qualityRows").addEventListener("click", async (event) => {
    const searchButton = event.target.closest("[data-quality-search]");
    if (searchButton) {
      const id = searchButton.dataset.qualitySearch;
      searchButton.disabled = true;
      searchButton.textContent = "Ищу…";
      try {
        const result = await api(`/api/uncensored/candidates/${encodeURIComponent(id)}`);
        qualityCandidates.set(id, result.candidates || []);
        renderQuality(qualityItems);
        if (!result.candidates.length) toast("Подходящий официальный кандидат не найден");
      } catch (err) {
        toast(err.message);
      } finally {
        const button = $("qualityRows").querySelector(`[data-quality-search="${CSS.escape(id)}"]`);
        if (button) {
          button.disabled = false;
          button.textContent = "Найти кандидатов";
        }
      }
      return;
    }

    const replaceButton = event.target.closest("[data-quality-replace]");
    if (!replaceButton) return;
    const id = replaceButton.dataset.qualityReplace;
    const candidates = qualityCandidates.get(id) || [];
    const select = $("qualityRows").querySelector(`[data-quality-choice="${CSS.escape(id)}"]`);
    const candidate = candidates[Number(select && select.value)] || candidates[0];
    const item = qualityItems.find((row) => row.id === id);
    if (!candidate || !item) return;
    if (!window.confirm(`Заменить «${item.artist} — ${item.title}» на «${candidate.title}»? Старый файл будет перемещён в архив.`)) return;

    replaceButton.disabled = true;
    showQualityProgress(0, "Загружаю и проверяю файл…", true);
    try {
      const result = await api(`/api/uncensored/replace/${encodeURIComponent(id)}`, {
        method: "POST",
        body: JSON.stringify({ confirmed: true, candidate_url: candidate.url }),
      });
      const library = await api("/api/library");
      applyLibrary(library);
      qualityItems = qualityItems.filter((row) => row.id !== id);
      qualityCandidates.delete(id);
      renderQuality(qualityItems);
      showQualityProgress(100, `Заменено. Оригинал в архиве: ${result.archive_path}`, false);
      toast("Трек заменён; оригинал сохранён в архиве");
    } catch (err) {
      replaceButton.disabled = false;
      showQualityProgress(0, err.message, false);
      toast(err.message);
    }
  });

  $("qualityReplaceAll").addEventListener("click", async () => {
    if (!qualityItems.length || !window.confirm(`Автоматически заменить ${qualityItems.length} подозрительных треков? Будут выбраны первые подходящие официальные версии. Исходные файлы сохранятся в архиве.`)) return;
    $("qualityReplaceAll").disabled = true;
    try {
      const started = await api("/api/uncensored/replace_all", {
        method: "POST",
        body: JSON.stringify({ confirmed: true }),
      });
      if (!started.started) {
        toast(started.message || "Массовая замена не запущена");
        return;
      }
      const poll = async () => {
        try {
          const status = await api("/api/uncensored/replace_all/status");
          const percent = status.total ? Math.round(status.processed * 100 / status.total) : 0;
          showQualityProgress(percent, `${status.processed}/${status.total}`, false);
          if (status.status === "running") {
            qualityPollTimer = setTimeout(poll, 1200);
            return;
          }
          const replaced = (status.results || []).filter((row) => row.status === "replaced").length;
          $("qualitySummary").textContent = `Массовая замена завершена: успешно ${replaced}, пропущено ${status.total - replaced}.`;
          showQualityProgress(100, "Готово", false);
          const library = await api("/api/library");
          applyLibrary(library);
          renderQuality(qualityItems.filter((item) => !(status.results || []).some((row) => row.track_id === item.id && row.status === "replaced")));
        } catch (err) {
          showQualityProgress(0, err.message, false);
          toast(err.message);
        }
      };
      await poll();
    } catch (err) {
      $("qualityReplaceAll").disabled = false;
      toast(err.message);
    }
  });

  // ---------- модалка выбора папки ----------
  let fsPath = "";

  function openModal() {
    $("modal").classList.remove("hidden");
    loadRoots();
  }
  function closeModal() {
    $("modal").classList.add("hidden");
  }
  $("btnBrowse").addEventListener("click", openModal);
  $("btnBrowseHero").addEventListener("click", openModal);
  $("modalClose").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => {
    if (e.target.id === "modal") closeModal();
  });

  async function loadRoots() {
    const data = await api("/api/fs/roots");
    $("fsRoots").innerHTML = data.roots
      .map(
        (r) =>
          `<button data-path="${esc(r.path)}">${esc(r.name)}</button>`
      )
      .join("");
    const start = data.roots[0] && data.roots[0].path;
    if (start) browse(start);
  }

  $("fsRoots").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) browse(b.dataset.path);
  });

  async function browse(path) {
    try {
      const data = await api("/api/fs/list?path=" + encodeURIComponent(path));
      fsPath = data.path;
      $("crumbs").textContent = data.path;
      $("fsHint").textContent = data.music_count
        ? `Аудиофайлов в этой папке: ${data.music_count}`
        : "Аудиофайлов в корне не видно (могут быть во вложенных)";
      $("fsOpen").disabled = false;
      const up = data.parent
        ? `<div class="fs-item" data-path="${esc(data.parent)}"><span>↑  ..</span></div>`
        : "";
      $("fsList").innerHTML =
        up +
        data.dirs
          .map(
            (d) =>
              `<div class="fs-item" data-path="${esc(d.path)}"><span>📁  ${esc(
                d.name
              )}</span></div>`
          )
          .join("");
    } catch (err) {
      toast(err.message);
    }
  }

  $("fsList").addEventListener("click", (e) => {
    const it = e.target.closest(".fs-item");
    if (it) browse(it.dataset.path);
  });
  $("fsList").addEventListener("dblclick", (e) => {
    const it = e.target.closest(".fs-item");
    if (it) browse(it.dataset.path);
  });
  $("fsOpen").addEventListener("click", () => openFolder(fsPath));

  // ---------- живые события резолвера ----------
  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (_) {
        return;
      }
      if (msg.type === "hello") setOllamaPill(msg.ollama);
      if (msg.type === "resolved") {
        const t = state.tracks.find((x) => x.id === msg.track_id);
        if (t && msg.media) {
          t.media = msg.media;
          clipCache.set(t.id, msg.media);
          if (!state.coverScrollLock) virtPaint();
        }
        if (msg.total) {
          $("batchProgress").textContent = `${msg.index}/${msg.total}`;
        }
      }
      if (msg.type === "batch_done") {
        $("batchProgress").textContent = "готово";
        toast("Клипы и обложки обновлены");
        setTimeout(() => ($("batchProgress").textContent = ""), 2500);
      }
    };
    ws.onclose = () => setTimeout(connectWs, 2500);
    setInterval(() => {
      if (ws.readyState === 1) ws.send("ping");
    }, 25000);
  }

  // ---------- текст песни ----------
  async function loadLyrics(track, gen) {
    const panel = $("lyricsPanel");
    const scroll = $("lyricsScroll");
    const follow = $("btnLyricsFollow");
    state.lyrics = null;
    state.lyricIndex = -1;
    state.lyricFollow = true;
    panel.classList.add("hidden");
    panel.classList.remove("manual");
    if (follow) follow.classList.add("hidden");
    $("mediaLayer").classList.remove("has-lyrics");
    scroll.innerHTML = "";
    scroll.style.transform = "none";
    try {
      let data = lyricsCache.get(track.id);
      if (!data) data = await api(`/api/lyrics/${track.id}`);
      if (gen !== undefined && gen !== state.playGen) return;
      if (!data || !data.ok || !data.lines || !data.lines.length) {
        toast("Текст песни не найден");
        return;
      }
      lyricsCache.set(track.id, data);
      state.lyrics = data;
      panel.classList.remove("hidden");
      panel.classList.toggle("plain", !data.synced);
      $("mediaLayer").classList.add("has-lyrics");
      const note =
        data.timing === "estimated"
          ? `<div class="lyrics-note">примерная синхронизация</div>`
          : data.synced
            ? ""
            : `<div class="lyrics-note">текст без синхронизации</div>`;
      scroll.style.transform = "none";
      scroll.innerHTML =
        note +
        data.lines
          .map((ln, i) => {
            const words =
              ln.words && ln.words.length
                ? ln.words
                    .map(
                      (w, j) =>
                        `<span class="lyric-word" data-t="${w.t}" data-j="${j}">${esc(w.text)}</span>`
                    )
                    .join(" ")
                : esc(ln.text);
            return `<div class="lyric-line" data-i="${i}" data-t="${ln.t ?? ""}">${words}</div>`;
          })
          .join("");
      if (data.synced) syncLyrics(0);
    } catch (error) {
      console.warn("Lyrics lookup failed:", error);
      if (gen === undefined || gen === state.playGen) {
        toast("Не удалось загрузить текст песни");
      }
    }
  }

  function moveLyrics(idx) {
    const win = $("lyricsWindow");
    const scroll = $("lyricsScroll");
    const el = scroll && scroll.querySelectorAll(".lyric-line")[idx];
    if (!win || !el) return;
    if (!state.lyricFollow) return;
    if (performance.now() < (state.lyricPauseUntil || 0)) return;
    scroll.style.transform = "none";
    const top = el.offsetTop - win.clientHeight / 2 + el.offsetHeight / 2;
    win.scrollTop = Math.max(0, top);
  }

  function syncLyrics(pos) {
    const data = state.lyrics;
    const scroll = $("lyricsScroll");
    if (!data || !data.lines.length || !scroll) return;
    if (!data.synced) return;
    let idx = 0;
    for (let i = 0; i < data.lines.length; i++) {
      const t = data.lines[i].t;
      if (t == null) continue;
      if (t <= pos + 0.05) idx = i;
      else break;
    }
    const nodes = scroll.querySelectorAll(".lyric-line");
    const paused = performance.now() < (state.lyricPauseUntil || 0);
    if (idx !== state.lyricIndex) {
      state.lyricIndex = idx;
      nodes.forEach((el, i) => {
        const far = i < idx - 2 || i > idx + 2;
        el.classList.toggle("active", i === idx);
        el.classList.toggle("near", i === idx - 1 || i === idx + 1);
        el.classList.toggle("past", i < idx && !far);
        el.classList.toggle("far", far);
      });
      if (!paused) moveLyrics(idx);
    }
    if (!paused) {
      const panel = $("lyricsPanel");
      if (panel.classList.contains("manual")) {
        panel.classList.remove("manual");
        moveLyrics(idx);
        const follow = $("btnLyricsFollow");
        if (follow) follow.classList.add("hidden");
      }
    }
    const line = data.lines[idx];
    const node = nodes[idx];
    const nowW = performance.now();
    if (line && line.words && line.words.length && node && nowW - (state._wordTick || 0) > 100) {
      state._wordTick = nowW;
      let wi = 0;
      for (let j = 0; j < line.words.length; j++) {
        if (line.words[j].t <= pos + 0.02) wi = j;
      }
      node.querySelectorAll(".lyric-word").forEach((el, j) => {
        el.classList.toggle("active-word", j === wi);
        el.classList.toggle("on", j === wi);
        el.classList.toggle("dimmed", j !== wi);
      });
      const fill = ((wi + 1) / line.words.length) * 100;
      node.style.setProperty("--fill", fill + "%");
    } else if (node && line && !(line.words && line.words.length)) {
      const next = data.lines[idx + 1];
      const t0 = line.t || 0;
      const t1 = next && next.t != null ? next.t : t0 + 4;
      const p = Math.max(0, Math.min(1, (pos - t0) / Math.max(0.2, t1 - t0)));
      node.style.setProperty("--fill", p * 100 + "%");
    }
  }

  // ---------- цвет из обложки ----------
  function applyPalette(c1, c2, c3) {
    const css = (x) => `rgb(${x.r},${x.g},${x.b})`;
    const root = document.documentElement;
    root.style.setProperty("--glow", `${c1.r}, ${c1.g}, ${c1.b}`);
    root.style.setProperty("--accent", css({
      r: Math.min(255, c1.r + 36),
      g: Math.min(255, c1.g + 36),
      b: Math.min(255, c1.b + 36),
    }));
    root.style.setProperty("--accent-1", css(c1));
    root.style.setProperty("--accent-2", css(c2));
    root.style.setProperty("--accent-3", css(c3));
  }
  function paintFromCover(img) {
    try {
      const tid = currentTrack() && currentTrack().id;
      if (tid) {
        try {
          const raw = localStorage.getItem("kadr-palette-" + tid);
          if (raw) {
            const pal = JSON.parse(raw);
            applyPalette(pal.c1, pal.c2, pal.c3);
            return;
          }
        } catch (_) {}
      }
      const c = document.createElement("canvas");
      c.width = 64;
      c.height = 64;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, 64, 64);
      const data = ctx.getImageData(0, 0, 64, 64).data;
      const buckets = [];
      let r = 0, g = 0, b = 0, n = 0;
      for (let i = 0; i < data.length; i += 4) {
        const rr = data[i], gg = data[i + 1], bb2 = data[i + 2], a = data[i + 3];
        if (a < 200) continue;
        const mx = Math.max(rr, gg, bb2), mn = Math.min(rr, gg, bb2);
        if (mx < 28) continue;
        r += rr; g += gg; b += bb2; n++;
        buckets.push({ r: rr, g: gg, b: bb2, s: mx - mn, l: mx });
      }
      if (!n) return;
      buckets.sort((x, y) => y.s - x.s || y.l - x.l);
      const c1 = buckets[0];
      const c2 = buckets[Math.min(buckets.length - 1, Math.floor(buckets.length * 0.25))];
      const c3 = { r: Math.round(r / n), g: Math.round(g / n), b: Math.round(b / n) };
      applyPalette(c1, c2, c3);
      if (tid) {
        try { localStorage.setItem("kadr-palette-" + tid, JSON.stringify({ c1, c2, c3 })); } catch (_) {}
      }
    } catch (_) {}
  }

  // блик стекла — CSS :hover, без pointermove

  // ---------- PWA ----------
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    state.deferredInstall = e;
    $("btnInstall").hidden = false;
  });
  $("btnInstall").addEventListener("click", async () => {
    if (state.deferredInstall) {
      state.deferredInstall.prompt();
      await state.deferredInstall.userChoice;
      state.deferredInstall = null;
      $("btnInstall").hidden = true;
      return;
    }
    toast("Ярлык: запустите install_shortcut.bat в папке Курымдык");
  });

  // ---------- текст: клик = перемотка, ручной скролл ----------
  $("lyricsScroll").addEventListener("click", (e) => {
    const word = e.target.closest(".lyric-word");
    const line = e.target.closest(".lyric-line");
    const t = word ? Number(word.dataset.t) : line ? Number(line.dataset.t) : NaN;
    if (!isFinite(t)) return;
    e.stopPropagation();
    seekTo(t);
    state.lyricFollow = true;
    state.lyricUserScroll = false;
    const follow = $("btnLyricsFollow");
    if (follow) follow.classList.add("hidden");
  });
  $("lyricsWindow").addEventListener(
    "wheel",
    () => {
      state.lyricUserScroll = true;
      state.lyricPauseUntil = performance.now() + 3000;
      $("lyricsPanel").classList.add("manual");
      const follow = $("btnLyricsFollow");
      if (follow) follow.classList.remove("hidden");
    },
    { passive: true }
  );
  $("lyricsWindow").addEventListener(
    "pointerdown",
    () => {
      state.lyricUserScroll = true;
    },
    { passive: true }
  );
  $("lyricsWindow").addEventListener(
    "scroll",
    () => {
      if (!state.lyricUserScroll) return;
      state.lyricFollow = false;
      state.lyricPauseUntil = performance.now() + 3000;
      $("lyricsPanel").classList.add("manual");
      const follow = $("btnLyricsFollow");
      if (follow) follow.classList.remove("hidden");
    },
    { passive: true }
  );
  $("btnLyricsFollow").addEventListener("click", (e) => {
    e.stopPropagation();
    state.lyricFollow = true;
    state.lyricUserScroll = false;
    e.currentTarget.classList.add("hidden");
    state.lyricIndex = -1;
    syncLyrics(getPosition());
  });

  // ---------- инерция плейлиста + свайп сцены (transform/opacity) ----------
  (function playlistInertia() {
    const view = $("playlistView");
    if (!view) return;
    let vy = 0,
      lastY = 0,
      lastT = 0,
      down = false,
      raf = 0,
      moved = 0;
    view.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse") return;
      down = true;
      vy = 0;
      moved = 0;
      lastY = e.clientY;
      lastT = performance.now();
      if (raf) cancelAnimationFrame(raf);
      view.setPointerCapture(e.pointerId);
    });
    view.addEventListener("pointermove", (e) => {
      if (!down) return;
      const now = performance.now();
      const dy = e.clientY - lastY;
      const dt = Math.max(8, now - lastT);
      view.scrollTop -= dy;
      vy = dy / dt;
      lastY = e.clientY;
      lastT = now;
      moved += Math.abs(dy);
    });
    function fling() {
      raf = 0;
      if (Math.abs(vy) < 0.04) return;
      view.scrollTop -= vy * 16;
      vy *= 0.935;
      raf = requestAnimationFrame(fling);
    }
    view.addEventListener("pointerup", () => {
      down = false;
      if (Math.abs(vy) > 0.12 && moved > 8) raf = requestAnimationFrame(fling);
    });
    view.addEventListener("pointercancel", () => {
      down = false;
    });
  })();

  (function stageSwipe() {
    const stage = $("artStage");
    const layer = $("mediaLayer");
    if (!stage || !layer) return;
    let x0 = 0,
      y0 = 0,
      t0 = 0,
      tracking = false,
      axis = null;
    stage.addEventListener("pointerdown", (e) => {
      if (e.target.closest("button") || e.target.closest(".lyrics")) return;
      tracking = true;
      axis = null;
      x0 = e.clientX;
      y0 = e.clientY;
      t0 = performance.now();
    });
    stage.addEventListener("pointermove", (e) => {
      if (!tracking) return;
      const dx = e.clientX - x0;
      const dy = e.clientY - y0;
      if (!axis && Math.hypot(dx, dy) > 14) {
        axis = Math.abs(dx) > Math.abs(dy) * 1.15 ? "x" : "y";
      }
      if (axis !== "x") return;
      e.preventDefault();
      layer.style.transition = "none";
      layer.style.transform = `translate3d(${dx * 0.42}px,0,0)`;
      layer.style.opacity = String(
        Math.max(0.45, 1 - (Math.abs(dx) / (stage.clientWidth || 1)) * 0.5)
      );
    });
    function finish(e) {
      if (!tracking) return;
      tracking = false;
      const dx = e.clientX - x0;
      const dt = Math.max(1, performance.now() - t0);
      const w = stage.clientWidth || 1;
      const flick =
        axis === "x" && (Math.abs(dx) > w * 0.16 || (Math.abs(dx) > 40 && dt < 260));
      layer.style.transition = "transform 0.28s ease, opacity 0.28s ease";
      if (flick) {
        const dir = dx < 0 ? 1 : -1;
        layer.style.transform = `translate3d(${dir * -w * 0.45}px,0,0)`;
        layer.style.opacity = "0";
        setTimeout(() => {
          if (dir > 0) next(false);
          else prev();
          layer.style.transition = "none";
          layer.style.transform = `translate3d(${dir * w * 0.22}px,0,0)`;
          layer.style.opacity = "0";
          requestAnimationFrame(() => {
            layer.style.transition = "transform 0.3s ease, opacity 0.3s ease";
            layer.style.transform = "translate3d(0,0,0)";
            layer.style.opacity = "1";
          });
        }, 160);
      } else {
        layer.style.transform = "translate3d(0,0,0)";
        layer.style.opacity = "1";
      }
      axis = null;
      setTimeout(() => {
        layer.style.transition = "";
      }, 400);
    }
    stage.addEventListener("pointerup", finish);
    stage.addEventListener("pointercancel", finish);
    document.addEventListener("pointerdown", (e) => {
      if (e.clientX < 18 && state.collapsed) setCollapsed(false);
    });
  })();

  // ---------- сайдбар: ширина и сворачивание ----------
  function setCollapsed(on) {
    state.collapsed = on;
    document.documentElement.classList.toggle("collapsed", on);
    $("btnExpand").classList.toggle("hidden", !on);
    requestAnimationFrame(() => virtPaint());
  }
  $("btnCollapse").addEventListener("click", () => setCollapsed(true));
  $("btnExpand").addEventListener("click", () => setCollapsed(false));

  (function sidebarDrag() {
    const handle = $("sideHandle");
    let drag = false;
    handle.addEventListener("pointerdown", (e) => {
      if (state.collapsed) {
        setCollapsed(false);
        return;
      }
      drag = true;
      handle.classList.add("drag");
      handle.setPointerCapture(e.pointerId);
    });
    handle.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const layout = $("layout").getBoundingClientRect();
      const x = e.clientX - layout.left;
      const min = layout.width * 0.14;
      const max = layout.width * 0.38;
      const w = Math.min(max, Math.max(min, x));
      document.documentElement.style.setProperty("--side", (w / layout.width) * 100 + "vw");
    });
    handle.addEventListener("pointerup", () => {
      drag = false;
      handle.classList.remove("drag");
    });
  })();

  function isFs() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }
  function fsTarget() {
    return $("viewport") || $("artStage");
  }
  function toggleFullscreen(force) {
    const el = fsTarget();
    if (!el) return;
    const on = force === undefined ? !isFs() : !!force;
    if (on) {
      const req = el.requestFullscreen || el.webkitRequestFullscreen;
      if (req) {
        const p = req.call(el);
        if (p && p.catch) p.catch(() => {});
      }
    } else if (isFs()) {
      const ex = document.exitFullscreen || document.webkitExitFullscreen;
      if (ex) {
        const p = ex.call(document);
        if (p && p.catch) p.catch(() => {});
      }
    }
  }
  let fsHideT = 0;
  function bumpFsBar() {
    const bar = $("fsControls");
    if (!bar) return;
    bar.classList.remove("hide");
    clearTimeout(fsHideT);
    if (!isFs()) return;
    fsHideT = setTimeout(() => {
      if (isFs()) bar.classList.add("hide");
    }, 3000);
  }
  function syncFsChrome() {
    const on = isFs();
    const b = $("btnFs");
    if (b) b.textContent = on ? "✕" : "⛶";
    const bar = $("fsControls");
    if (bar && !on) bar.classList.remove("hide");
    if (on) bumpFsBar();
  }
  document.addEventListener("fullscreenchange", syncFsChrome);
  document.addEventListener("webkitfullscreenchange", syncFsChrome);
  const _fsHost = $("artStage") || $("viewport");
  if (_fsHost) {
    _fsHost.addEventListener("pointermove", bumpFsBar);
    _fsHost.addEventListener("mousemove", bumpFsBar);
  }
  $("btnFs").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleFullscreen();
  });
  $("artStage").addEventListener("dblclick", (e) => {
    if (e.target.closest(".lyrics") || e.target.closest("button") || e.target.closest("#fsControls")) return;
    toggleFullscreen();
  });
  const fsSeek = $("fsSeek");
  if (fsSeek) {
    fsSeek.addEventListener("input", () => { state.seeking = true; bumpFsBar(); });
    fsSeek.addEventListener("change", () => {
      const d = getDuration();
      seekTo((Number(fsSeek.value) / 1000) * d);
      state.seeking = false;
      bumpFsBar();
    });
  }
  const fsPrev = $("fsPrev");
  const fsPlay = $("fsPlay");
  const fsNext = $("fsNext");
  if (fsPrev) fsPrev.addEventListener("click", (e) => { e.stopPropagation(); prev(); bumpFsBar(); });
  if (fsPlay) fsPlay.addEventListener("click", (e) => { e.stopPropagation(); togglePlay(); bumpFsBar(); });
  if (fsNext) fsNext.addEventListener("click", (e) => { e.stopPropagation(); next(false); bumpFsBar(); });
  window.addEventListener("resize", () => {
    if (state.virtRaf) return;
    state.virtRaf = requestAnimationFrame(() => {
      state.virtRaf = 0;
      virtPaint();
    });
  });


  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn, .icon-btn");
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const span = document.createElement("span");
    span.className = "ripple";
    const size = Math.max(r.width, r.height) * 2.4;
    span.style.width = span.style.height = size + "px";
    span.style.left = (e.clientX - r.left - size / 2) + "px";
    span.style.top = (e.clientY - r.top - size / 2) + "px";
    btn.appendChild(span);
    setTimeout(() => span.remove(), 620);
  });

  (function magnetic() {
    ["btnPlay", "btnNext", "btnPrev"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("pointermove", (e) => {
        if (e.pointerType !== "mouse") return;
        const r = el.getBoundingClientRect();
        const dx = Math.max(-6, Math.min(6, e.clientX - (r.left + r.width / 2)));
        const dy = Math.max(-6, Math.min(6, e.clientY - (r.top + r.height / 2)));
        el.style.transform = "translate(" + (dx * 0.35) + "px," + (dy * 0.35) + "px)";
      });
      el.addEventListener("pointerleave", () => { el.style.transform = ""; });
    });
  })();

  (function parallax() {
    const stage = $("artStage");
    const vinyl = $("vinyl");
    if (!stage || !vinyl) return;
    let tx = 0, ty = 0, cx = 0, cy = 0, raf = 0;
    function tickP() {
      raf = 0;
      cx += (tx - cx) * 0.12;
      cy += (ty - cy) * 0.12;
      if (state.mode !== "youtube") vinyl.style.translate = cx + "px " + cy + "px";
      if (Math.abs(cx - tx) + Math.abs(cy - ty) > 0.1) raf = requestAnimationFrame(tickP);
    }
    stage.addEventListener("pointermove", (e) => {
      if (e.pointerType !== "mouse") return;
      const r = stage.getBoundingClientRect();
      tx = ((e.clientX - r.left) / r.width - 0.5) * 16;
      ty = ((e.clientY - r.top) / r.height - 0.5) * 12;
      if (!raf) raf = requestAnimationFrame(tickP);
    });
    stage.addEventListener("pointerleave", () => {
      tx = 0; ty = 0;
      if (!raf) raf = requestAnimationFrame(tickP);
    });
  })();

  const fxDots = [];
  (function fxPool() {
    const pool = $("fxPool");
    if (!pool) return;
    for (let i = 0; i < 20; i++) {
      const d = document.createElement("div");
      d.className = "fx-dot";
      pool.appendChild(d);
      fxDots.push({ el: d, busy: false });
    }
  })();
  let lastBassHit = 0;
  function burstBass(x, y) {
    const now = performance.now();
    if (now - lastBassHit < 180) return;
    lastBassHit = now;
    let n = 0;
    fxDots.forEach((p) => {
      if (p.busy || n >= 4) return;
      p.busy = true;
      n++;
      const ang = Math.random() * Math.PI * 2;
      const dist = 40 + Math.random() * 90;
      p.el.style.left = x + "px";
      p.el.style.top = y + "px";
      p.el.style.opacity = "1";
      p.el.style.transition = "none";
      p.el.style.transform = "translate(0,0) scale(1)";
      requestAnimationFrame(() => {
        p.el.style.transition = "transform 800ms ease-out, opacity 800ms ease-out";
        p.el.style.transform = "translate(" + (Math.cos(ang) * dist) + "px," + (Math.sin(ang) * dist) + "px) scale(0.2)";
        p.el.style.opacity = "0";
      });
      setTimeout(() => { p.busy = false; }, 820);
    });
  }

  const waveCache = {};
  async function buildWave(trackId) {
    if (!trackId) return;
    if (waveCache[trackId] && waveCache[trackId] !== true) {
      drawWave(waveCache[trackId]);
      return;
    }
    if (waveCache[trackId] === true) return;
    try {
      const ls = localStorage.getItem("kadr-wave-" + trackId);
      if (ls) {
        waveCache[trackId] = JSON.parse(ls);
        drawWave(waveCache[trackId]);
        return;
      }
    } catch (_) {}
    waveCache[trackId] = true;
    try {
      const buf = await fetch("/api/stream/" + trackId).then((r) => r.arrayBuffer());
      const ac = new (window.AudioContext || window.webkitAudioContext)();
      const audioBuf = await ac.decodeAudioData(buf.slice(0));
      const ch = audioBuf.getChannelData(0);
      const n = 512;
      const block = Math.max(1, Math.floor(ch.length / n));
      const peaks = new Array(n);
      for (let i = 0; i < n; i++) {
        let m = 0;
        const off = i * block;
        for (let j = 0; j < block; j += 12) m = Math.max(m, Math.abs(ch[off + j] || 0));
        peaks[i] = Math.min(1, m * 1.6);
      }
      waveCache[trackId] = peaks;
      try { localStorage.setItem("kadr-wave-" + trackId, JSON.stringify(peaks)); } catch (_) {}
      drawWave(peaks);
      ac.close();
    } catch (_) {
      waveCache[trackId] = null;
    }
  }
  function drawWave(peaks) {
    const c = $("waveSeek");
    if (!c || !peaks || peaks === true) return;
    const ctx = c.getContext("2d");
    const w = c.width, h = c.height;
    ctx.clearRect(0, 0, w, h);
    const d = getDuration() || 1;
    const p = getPosition() / d;
    const acc = getComputedStyle(document.documentElement).getPropertyValue("--accent-1").trim() || "#fff";
    const n = peaks.length;
    const bw = w / n;
    for (let i = 0; i < n; i++) {
      const bh = Math.max(2, peaks[i] * (h - 4));
      ctx.fillStyle = i / n < p ? acc : "rgba(255,255,255,0.15)";
      ctx.fillRect(i * bw, (h - bh) / 2, Math.max(1, bw - 0.4), bh);
    }
  }
  const waveEl = $("waveSeek");
  if (waveEl) waveEl.addEventListener("click", (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const d = getDuration();
    if (d) seekTo(x * d);
  });

  const ICO_MOON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M21 14.3A8.5 8.5 0 1 1 9.7 3 7 7 0 0 0 21 14.3z"/></svg>';
  const ICO_SUN = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4" fill="currentColor"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round"/></svg>';

  function applyTheme(t) {
    const theme = t === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("kadr-theme", theme); } catch (_) {}
    const btn = $("btnTheme");
    if (btn) {
      btn.innerHTML = theme === "light" ? ICO_SUN : ICO_MOON;
      btn.title = theme === "light" ? "Светлая тема" : "Тёмная тема";
    }
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "light" ? "#e8e4dc" : "#0a0a0c");
  }

  (function themeToggle() {
    const btn = $("btnTheme");
    if (!btn) return;
    applyTheme(document.documentElement.getAttribute("data-theme") || "dark");
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      applyTheme(cur === "dark" ? "light" : "dark");
    });
  })();

  function updateNowPlaying() {
    const bar = $("nowPlayingBar");
    const title = $("npTitle");
    const t = currentTrack();
    if (!bar) return;
    if (!t) {
      bar.classList.add("hidden");
      return;
    }
    bar.classList.remove("hidden");
    if (title) title.textContent = t.artist + " — " + t.title;
  }

  function flashActive() {
    const ul = $("playlist");
    if (!ul) return;
    const li = ul.querySelector("li.active");
    if (!li) return;
    li.classList.remove("flash");
    void li.offsetWidth;
    li.classList.add("flash");
    setTimeout(() => li.classList.remove("flash"), 800);
  }

  const npJump = $("npJump");
  if (npJump) npJump.addEventListener("click", (e) => {
    e.stopPropagation();
    setSideTab("tracks");
    scrollToActive();
    requestAnimationFrame(flashActive);
  });

  function filterByArtist(name, stay) {
    state.artistFilter = name || "";
    $("search").value = name || "";
    if (!stay) setSideTab("tracks");
    $("playlistView").scrollTop = 0;
    renderPlaylist();
    const all = $("btnAllTracks");
    if (all) all.classList.toggle("hidden", !name);
  }

  function syncFavoritesFilter() {
    const button = $("btnFavoritesFilter");
    if (!button) return;
    button.classList.toggle("on", state.favoritesOnly);
    button.setAttribute("aria-pressed", String(state.favoritesOnly));
    button.title = state.favoritesOnly ? "Показать все треки" : "Показать избранное";
    button.setAttribute("aria-label", button.title);
  }

  function setSideTab(tab) {
    state.sideTab = tab === "artists" ? "artists" : "tracks";
    const tracksOn = state.sideTab === "tracks";
    $("tabTracks").classList.toggle("on", tracksOn);
    $("tabArtists").classList.toggle("on", !tracksOn);
    $("tabTracks").setAttribute("aria-selected", String(tracksOn));
    $("tabArtists").setAttribute("aria-selected", String(!tracksOn));
    $("playlistView").classList.toggle("hidden", !tracksOn);
    const av = $("artistsView");
    if (av) av.classList.toggle("hidden", tracksOn);
    if (!tracksOn) loadArtistsPanel();
    else requestAnimationFrame(() => virtPaint());
  }

  function renderArtistsList(selected) {
    const ul = $("artistsList");
    if (!ul) return;
    ul.innerHTML = (state.artists || []).map((a) => {
      const followed = state.followedArtists.has(a.name);
      const query = encodeURIComponent(a.name);
      return `<li class="artist-row ${a.name === selected ? "on" : ""}">` +
        `<button class="artist-main" type="button" data-name="${esc(a.name)}">` +
        `<span class="a-name">${esc(a.name)}</span><span class="a-count">${a.count}</span></button>` +
        `<button class="icon-btn sm artist-follow ${followed ? "on" : ""}" type="button" ` +
        `data-follow-artist="${esc(a.name)}" title="${followed ? "Убрать из избранных артистов" : "Следить за артистом"}" ` +
        `aria-label="${followed ? "Убрать из избранных артистов" : "Добавить артиста в избранное"}" aria-pressed="${followed}">` +
        `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg></button>` +
        `<a class="artist-source" href="https://soundcloud.com/search?q=${query}" target="_blank" rel="noopener noreferrer" title="Искать старые записи на SoundCloud" aria-label="Искать ${esc(a.name)} на SoundCloud">SC</a>` +
        `</li>`;
    }).join("");
  }

  function showSimilar(name) {
    const box = $("similarBox");
    const host = $("similarNames");
    if (!box || !host) return;
    const a = (state.artists || []).find((x) => x.name === name);
    const sim = (a && a.similar) || [];
    if (!sim.length) {
      box.classList.add("hidden");
      host.innerHTML = "";
      return;
    }
    box.classList.remove("hidden");
    host.innerHTML = sim.map((n) => `<button type="button" class="similar-chip" data-name="${esc(n)}">${esc(n)}</button>`).join("");
  }

  function renderWeeklyReleases(data) {
    const tracksHost = $("newTracksList");
    const albumsHost = $("newAlbumsList");
    const status = $("releaseStatus");
    if (!tracksHost || !albumsHost || !status) return;
    const tracks = (data && data.tracks) || [];
    const albums = (data && data.albums) || [];
    const total = tracks.length + albums.length;
    if (!total) {
      tracksHost.innerHTML = "";
      albumsHost.innerHTML = "";
      status.textContent = data && data.errors && data.errors.length
        ? "Не удалось проверить некоторые источники"
        : "Новых релизов за последние 7 дней нет";
      return;
    }
    status.textContent = `${tracks.length} треков · ${albums.length} альбомов · Deezer`;
    const row = (release, withPreview) => {
      const date = new Date(`${release.date}T00:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
      const preview = withPreview && release.preview
        ? `<audio class="release-preview" controls preload="none" src="${esc(release.preview)}" aria-label="Предпрослушать ${esc(release.title)}"></audio>`
        : "";
      return `<li class="release-row">` +
        `${release.cover ? `<img class="release-cover" src="${esc(release.cover)}" alt="" loading="lazy">` : `<span class="release-cover missing" aria-hidden="true"></span>`}` +
        `<div class="release-info"><a class="release-title" href="${esc(release.url)}" target="_blank" rel="noopener noreferrer">${esc(release.title)}</a>` +
        `<span class="release-artist">${esc(release.artist)} · ${esc(date)}</span>${preview}</div>` +
        `<a class="artist-source" href="${esc(release.url)}" target="_blank" rel="noopener noreferrer" title="Открыть официальный релиз в Deezer" aria-label="Открыть ${esc(release.title)} в Deezer">↗</a>` +
        `</li>`;
    };
    tracksHost.innerHTML = tracks.map((release) => row(release, true)).join("");
    albumsHost.innerHTML = albums.map((release) => row(release, false)).join("");
    tracksHost.classList.toggle("hidden", state.releaseTab !== "tracks");
    albumsHost.classList.toggle("hidden", state.releaseTab !== "albums");
  }

  async function loadWeeklyReleases(force) {
    const status = $("releaseStatus");
    const counts = new Map();
    for (const track of state.tracks) {
      if (track.artist) counts.set(track.artist, (counts.get(track.artist) || 0) + 1);
    }
    const libraryArtists = [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([name]) => name);
    const names = [...new Set([...state.followedArtists, ...libraryArtists])].slice(0, 12);
    if (!names.length) {
      renderWeeklyReleases({ tracks: [], albums: [], errors: [] });
      if (status) status.textContent = "Выбери папку с музыкой";
      return;
    }
    const localTracks = [...new Map(state.tracks.map((track) => [
      `${track.artist}\0${track.title}\0${track.album}`,
      { artist: track.artist, title: track.title, album: track.album },
    ])).values()];
    const signature = `${names.slice().sort().join("\n")}\0${JSON.stringify(localTracks)}`;
    const key = "kurymdyk-weekly-releases";
    if (!force) {
      try {
        const cached = JSON.parse(localStorage.getItem(key) || "null");
        if (cached && cached.signature === signature && Date.now() - cached.savedAt < 6 * 60 * 60 * 1000) {
          renderWeeklyReleases(cached.data);
          return;
        }
      } catch (_) {}
    }
    if (status) status.textContent = `Проверяю ${names.length} артистов…`;
    try {
      const data = await api("/api/new-releases", {
        method: "POST",
        body: JSON.stringify({ artists: names, local_tracks: localTracks, days: 7 }),
      });
      renderWeeklyReleases(data);
      try { localStorage.setItem(key, JSON.stringify({ signature, savedAt: Date.now(), data })); } catch (_) {}
    } catch (_) {
      if (status) status.textContent = "Каталог релизов сейчас недоступен";
    }
  }

  function toggleFollowedArtist(name) {
    if (state.followedArtists.has(name)) state.followedArtists.delete(name);
    else state.followedArtists.add(name);
    try { localStorage.setItem(followedArtistsKey, JSON.stringify([...state.followedArtists])); } catch (_) {}
    renderArtistsList(state.artistFilter);
    loadWeeklyReleases(true);
  }

  async function loadArtistsPanel() {
    const status = $("artistStatus");
    if (state.artistsLoaded) {
      await loadWeeklyReleases(false);
      return;
    }
    if (status) status.textContent = "Подбираю похожих артистов…";
    try {
      const data = await api("/api/artists");
      state.artists = data.artists || [];
      state.artistsLoaded = true;
      renderArtistsList(state.artistFilter);
      if (status) status.textContent = data.ollama
        ? `${state.artists.length} артистов из твоей музыки`
        : `${state.artists.length} артистов из твоей музыки`;
      if (!data.ollama) {
        const box = $("similarBox");
        if (box) box.classList.add("hidden");
      }
      await loadWeeklyReleases(false);
    } catch (_) {
      if (status) status.textContent = "Не удалось загрузить артистов";
    }
  }

  const statsColors = ["#c7ad82", "#829fc7", "#8cb58c", "#bd8abc", "#d78572", "#6fb9b1", "#a9a0d8", "#d6c66a", "#8ea3a5", "#d28da3", "#92ad69", "#d19b67"];

  function renderStatsSummary(data) {
    for (const period of ["day", "week", "month", "all"]) {
      const item = data[period] || { tracks: 0, minutes: 0 };
      $(`stats${period[0].toUpperCase()}${period.slice(1)}`).textContent = `${item.tracks} треков`;
      $(`stats${period[0].toUpperCase()}${period.slice(1)}Time`).textContent = `${item.minutes} мин`;
    }
  }

  function renderStatsTop(data) {
    const renderList = (items, kind) => items.length
      ? items.map((item) => {
        const title = kind === "tracks" ? item.title : item.artist;
        const byline = kind === "tracks" ? item.artist : `${item.plays} прослушиваний`;
        return `<li><strong>${esc(title || "Без названия")}</strong><small>${esc(byline)} · ${item.plays} раз · ${item.minutes} мин</small></li>`;
      }).join("")
      : `<li class="stats-empty">Пока нет прослушиваний.</li>`;
    $("statsArtists").innerHTML = renderList(data.artists || [], "artists");
    $("statsTracks").innerHTML = renderList(data.tracks || [], "tracks");
  }

  function renderStatsTimeline(data) {
    const canvas = $("statsTimeline");
    if (!canvas) return;
    const bounds = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(bounds.width * ratio));
    canvas.height = Math.max(1, Math.floor(bounds.height * ratio));
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    const width = bounds.width;
    const height = bounds.height;
    const points = data.days || [];
    const values = points.map((point) => point.plays);
    const max = Math.max(1, ...values);
    const left = 26, right = 8, top = 12, bottom = 25;
    const chartWidth = width - left - right;
    const chartHeight = height - top - bottom;
    ctx.font = "11px system-ui";
    ctx.textBaseline = "middle";
    const muted = getComputedStyle(document.documentElement).getPropertyValue("--muted").trim() || "#888";
    ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--stroke").trim() || "rgba(255,255,255,.2)";
    ctx.fillStyle = muted;
    ctx.lineWidth = 1;
    for (let row = 0; row <= 3; row++) {
      const y = top + chartHeight * row / 3;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(width - right, y);
      ctx.stroke();
      ctx.textAlign = "right";
      const tick = max * (3 - row) / 3;
      ctx.fillText(Number.isInteger(tick) ? String(tick) : tick.toFixed(1), left - 5, y);
    }
    if (!values.some(Boolean)) {
      ctx.textAlign = "center";
      ctx.fillText("Прослушиваний пока нет", width / 2, height / 2);
    } else {
      ctx.strokeStyle = "#c7ad82";
      ctx.lineWidth = 2;
      ctx.beginPath();
      values.forEach((value, index) => {
        const x = left + (values.length <= 1 ? 0 : chartWidth * index / (values.length - 1));
        const y = top + chartHeight * (1 - value / max);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.fillStyle = "#c7ad82";
      values.forEach((value, index) => {
        if (!value) return;
        const x = left + (values.length <= 1 ? 0 : chartWidth * index / (values.length - 1));
        const y = top + chartHeight * (1 - value / max);
        ctx.beginPath();
        ctx.arc(x, y, 2.5, 0, Math.PI * 2);
        ctx.fill();
      });
    }
    ctx.fillStyle = muted;
    ctx.textAlign = "left";
    if (points.length) ctx.fillText(points[0].date.slice(5), left, height - 8);
    ctx.textAlign = "right";
    if (points.length) ctx.fillText(points[points.length - 1].date.slice(5), width - right, height - 8);

    const hours = data.hours || [];
    const maximumHour = Math.max(1, ...hours.map((hour) => hour.plays));
    $("statsHours").innerHTML = hours.map((hour) =>
      `<i class="stats-hour" style="height:${Math.max(2, Math.round(36 * hour.plays / maximumHour))}px" ` +
      `title="${String(hour.hour).padStart(2, "0")}:00 UTC · ${hour.plays} треков"></i>`).join("");
  }

  function renderStatsGenres(data) {
    const items = data.genres || [];
    const total = items.reduce((sum, item) => sum + item.weight, 0);
    const donut = $("statsDonut");
    const legend = $("statsGenres");
    if (!items.length || !total) {
      donut.style.background = "conic-gradient(var(--stroke) 0 100%)";
      legend.innerHTML = `<li class="stats-empty">${esc((data.warnings || [])[0] || "Жанры появятся с историей прослушиваний.")}</li>`;
      return;
    }
    let progress = 0;
    const stops = items.map((item, index) => {
      const start = progress;
      progress += item.weight / total * 100;
      return `${statsColors[index % statsColors.length]} ${start.toFixed(2)}% ${progress.toFixed(2)}%`;
    });
    donut.style.background = `conic-gradient(${stops.join(",")})`;
    donut.setAttribute("aria-label", `Жанры: ${items.map((item) => item.name).join(", ")}`);
    legend.innerHTML = items.map((item, index) =>
      `<li><i style="background:${statsColors[index % statsColors.length]}"></i><span>${esc(item.name)} · ${item.artists} исполн.</span></li>`).join("");
    if (data.warnings && data.warnings.length) {
      legend.insertAdjacentHTML("beforeend", `<li class="stats-empty">${esc(data.warnings[0])}</li>`);
    }
  }

  async function loadStats() {
    $("statsStatus").textContent = "Загружаю статистику…";
    try {
      const period = $("statsPeriod").value;
      const [summary, top, timeline, genres] = await Promise.all([
        api("/api/stats/summary"),
        api(`/api/stats/top?period=${encodeURIComponent(period)}`),
        api("/api/stats/timeline?days=30"),
        api("/api/stats/genres"),
      ]);
      renderStatsSummary(summary);
      renderStatsTop(top);
      renderStatsTimeline(timeline);
      renderStatsGenres(genres);
      $("statsStatus").textContent = `${summary.all.tracks} подтверждённых прослушиваний · время UTC`;
    } catch (error) {
      $("statsStatus").textContent = `Не удалось загрузить статистику: ${error.message}`;
    }
  }

  function openStats() {
    $("welcome").classList.add("hidden");
    $("mediaLayer").classList.add("hidden");
    $("artistProfile").classList.add("hidden");
    $("listeningStats").classList.remove("hidden");
    loadStats();
  }

  $("btnStats")?.addEventListener("click", openStats);
  $("statsBack")?.addEventListener("click", () => {
    $("listeningStats").classList.add("hidden");
    showWelcome(!currentTrack());
  });
  $("statsPeriod")?.addEventListener("change", () => {
    api(`/api/stats/top?period=${encodeURIComponent($("statsPeriod").value)}`)
      .then(renderStatsTop)
      .catch((error) => { $("statsStatus").textContent = `Не удалось обновить топ: ${error.message}`; });
  });

  function profileSource(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === "https:" && parsed.hostname === "www.last.fm"
        ? parsed.href
        : "";
    } catch (_) {
      return "";
    }
  }

  function renderArtistProfile(profile) {
    const content = $("profileContent");
    const status = $("profileStatus");
    if (!content || !status) return;
    status.textContent = `${profile.local_tracks.length} треков в библиотеке`;
    const image = profile.image
      ? `<img class="profile-image" src="${esc(profile.image)}" alt="" referrerpolicy="no-referrer">`
      : `<span class="profile-image missing" aria-hidden="true"></span>`;
    const genres = (profile.genres || []).map((tag) =>
      `<span class="profile-tag">${esc(tag.name)}</span>`).join("");
    const localRows = profile.local_tracks.length
      ? profile.local_tracks.map((track) =>
        `<li><button class="profile-track-main btn ghost" type="button" data-profile-track="${esc(track.id)}">` +
        `${esc(track.title)}<small>${esc([track.album, track.year].filter(Boolean).join(" · "))}</small></button></li>`).join("")
      : `<li class="muted">Треков в локальной библиотеке нет.</li>`;
    const topRows = (profile.top_tracks || []).length
      ? profile.top_tracks.map((track) => {
        const url = profileSource(track.url);
        const action = track.in_library
          ? `<span class="muted">В библиотеке</span>`
          : (url ? `<a class="profile-open" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Открыть в Last.fm ↗</a>` : "");
        return `<li><span class="profile-track-main">${esc(track.title)}<small>${esc(track.listeners)} слушателей</small></span>${action}</li>`;
      }).join("")
      : `<li class="muted">Last.fm не вернул топ-треки.</li>`;
    const similar = (profile.similar || []).map((artist) =>
      `<button class="similar-chip" type="button" data-profile-artist="${esc(artist.name)}">${esc(artist.name)}</button>`).join("");
    const releases = (profile.releases || []).length
      ? profile.releases.map((release) =>
        `<li><span class="profile-track-main">${esc(release.title)}<small>${esc(release.date)} · ${esc(release.type || "релиз")}</small></span>` +
        `<a class="profile-open" href="${esc(release.url)}" target="_blank" rel="noopener noreferrer">MusicBrainz ↗</a></li>`).join("")
      : `<li class="muted">Релизов за последние 3 месяца не найдено.</li>`;
    const warnings = (profile.warnings || []).map((warning) =>
      `<p class="profile-warning">${esc(warning)}</p>`).join("");
    const artistUrl = `https://www.last.fm/music/${encodeURIComponent(profile.artist)}`;
    content.innerHTML =
      `<div class="profile-hero">${image}<div><h1 class="profile-title">${esc(profile.artist)}</h1>` +
      `<div class="profile-tags">${genres || `<span class="muted">Жанры не указаны</span>`}</div>` +
      `<a class="profile-open" href="${esc(artistUrl)}" target="_blank" rel="noopener noreferrer">Профиль Last.fm ↗</a>` +
      `<p class="profile-bio">${esc(profile.bio || "Биография не указана.")}</p></div></div>` +
      `<div class="profile-sections">` +
      `<section class="profile-section"><h2>В библиотеке</h2><ul class="profile-list">${localRows}</ul></section>` +
      `<section class="profile-section"><h2>Топ треки · Last.fm</h2><ul class="profile-list">${topRows}</ul></section>` +
      `<section class="profile-section"><h2>Похожие исполнители</h2><div class="profile-similar">${similar || `<span class="muted">Нет данных.</span>`}</div></section>` +
      `<section class="profile-section"><h2>Новые релизы · 3 месяца</h2><ul class="profile-list">${releases}</ul>${warnings}</section>` +
      `</div>`;
  }

  async function openArtistProfile(name) {
    const profileView = $("artistProfile");
    if (!profileView || !name) return;
    $("welcome").classList.add("hidden");
    $("mediaLayer").classList.add("hidden");
    profileView.classList.remove("hidden");
    $("profileStatus").textContent = "Загружаю профиль…";
    $("profileContent").innerHTML = "";
    try {
      const profile = await api(`/api/artist/${encodeURIComponent(name)}`);
      renderArtistProfile(profile);
    } catch (error) {
      $("profileStatus").textContent = "Не удалось загрузить профиль";
      $("profileContent").innerHTML = `<p class="profile-warning">${esc(error.message)}</p>`;
    }
  }

  $("profileBack")?.addEventListener("click", () => {
    $("artistProfile").classList.add("hidden");
    showWelcome(!currentTrack());
  });
  $("profileContent")?.addEventListener("click", (event) => {
    const trackButton = event.target.closest("[data-profile-track]");
    if (trackButton) {
      const index = state.tracks.findIndex((track) => track.id === trackButton.dataset.profileTrack);
      if (index >= 0) {
        $("artistProfile").classList.add("hidden");
        playIndex(index);
      }
      return;
    }
    const artistButton = event.target.closest("[data-profile-artist]");
    if (artistButton) openArtistProfile(artistButton.dataset.profileArtist);
  });

  $("tabTracks")?.addEventListener("click", () => setSideTab("tracks"));
  $("tabArtists")?.addEventListener("click", () => setSideTab("artists"));
  $("btnAllTracks")?.addEventListener("click", () => filterByArtist(""));
  $("artistsList")?.addEventListener("click", (e) => {
    const follow = e.target.closest("[data-follow-artist]");
    if (follow) {
      e.preventDefault();
      e.stopPropagation();
      toggleFollowedArtist(follow.dataset.followArtist);
      return;
    }
    if (e.target.closest("a")) return;
    const main = e.target.closest(".artist-main");
    if (!main) return;
    const name = main.dataset.name;
    openArtistProfile(name);
  });
  $("similarNames")?.addEventListener("click", (e) => {
    const b = e.target.closest(".similar-chip");
    if (!b) return;
    openArtistProfile(b.dataset.name);
  });
  $("btnRefreshReleases")?.addEventListener("click", () => loadWeeklyReleases(true));
  $("tabNewTracks")?.addEventListener("click", () => {
    state.releaseTab = "tracks";
    $("tabNewTracks").classList.add("on");
    $("tabNewAlbums").classList.remove("on");
    $("tabNewTracks").setAttribute("aria-selected", "true");
    $("tabNewAlbums").setAttribute("aria-selected", "false");
    $("newTracksList").classList.remove("hidden");
    $("newAlbumsList").classList.add("hidden");
  });
  $("tabNewAlbums")?.addEventListener("click", () => {
    state.releaseTab = "albums";
    $("tabNewAlbums").classList.add("on");
    $("tabNewTracks").classList.remove("on");
    $("tabNewAlbums").setAttribute("aria-selected", "true");
    $("tabNewTracks").setAttribute("aria-selected", "false");
    $("newAlbumsList").classList.remove("hidden");
    $("newTracksList").classList.add("hidden");
  });

  // init
  vol.value = "85";
  applyVolume();
  loadHealth();
  connectWs();
  setInterval(() => { loadHealth(true); }, 30000);
})();
