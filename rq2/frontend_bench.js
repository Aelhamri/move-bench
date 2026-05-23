/*
 * RQ2 - Frontend bench instrumentation.
 *
 * A coller en bas de templates/index.html (ou inclus comme <script src=...>)
 * APRES la definition de tpPlayLoop / stibTp.
 *
 * Active via URL params :
 *   http://localhost:8090/?bench_limit=1000&bench_run=1&bench_duration=60
 *
 * Comportement :
 *   1. Si bench_limit est present dans l'URL :
 *      - fetch /api/bench/scenario/<limit>  (au lieu de /api/stib/trajectories)
 *      - log fetch_start / fetch_end / parse_end / first_frame
 *      - declenche play automatiquement
 *      - capture chaque frame de tpPlayLoop pendant bench_duration secondes
 *      - POST /api/bench/log avec les metriques
 *      - signale "BENCH_DONE" sur window pour Playwright
 */

(function() {
  const params = new URLSearchParams(window.location.search);
  const benchLimit = parseInt(params.get("bench_limit"), 10);
  if (Number.isNaN(benchLimit)) return;   // pas en mode bench

  const benchRunId   = parseInt(params.get("bench_run") || "1", 10);
  const benchDuration = parseFloat(params.get("bench_duration") || "60");

  console.log(`[bench] mode actif limit=${benchLimit} run=${benchRunId} duration=${benchDuration}s`);

  const benchState = {
    limit:           benchLimit,
    run_id:          benchRunId,
    duration_s:      benchDuration,
    fetch_start_ms:  null,
    fetch_end_ms:    null,
    parse_end_ms:    null,
    first_frame_ms:  null,
    payload_bytes:   -1,
    n_trips:         0,
    frames:          [],
    play_start_ms:   null,
    last_frame_ms:   null,
    done:            false,
  };

  // --- Patch tpPlayLoop pour capturer les metriques ---
  // On enveloppe la fonction originale.
  const originalPlayLoop = window.tpPlayLoop;
  if (typeof originalPlayLoop !== "function") {
    console.error("[bench] tpPlayLoop introuvable - le snippet doit etre charge APRES le code temporal player");
    return;
  }

  window.tpPlayLoop = function(nowWall) {
    const t_frame_start = performance.now();
    const result = originalPlayLoop.apply(this, arguments);
    const t_frame_end = performance.now();

    if (benchState.first_frame_ms === null && benchState.play_start_ms !== null) {
      benchState.first_frame_ms = t_frame_end;
    }

    if (benchState.play_start_ms !== null && !benchState.done) {
      const elapsed_s = (t_frame_end - benchState.play_start_ms) / 1000;

      // Compter les vehicules visibles via la source
      let visible = 0;
      try {
        const src = (typeof map !== "undefined") ? map.getSource("stib_vehicles") : null;
        if (src && src._data && src._data.features) {
          visible = src._data.features.length;
        }
      } catch (e) { /* ignore */ }

      // v2 (post-3-agents review) : 2 métriques distinctes
      //   frame_cpu_ms     = coût JS pur dans tpPlayLoop (setData, calculs)
      //   frame_interval_ms = délai entre 2 entrées de tpPlayLoop (≈ cadence rAF
      //                       = frame budget perçu, vsync-aligned, inclut compositing
      //                       implicite). C'est la métrique symétrique avec QGIS
      //                       SequentialJob.
      const prev_start = benchState.last_frame_start_ms;
      benchState.frames.push({
        frame_idx:         benchState.frames.length,
        elapsed_s:         elapsed_s,
        frame_cpu_ms:      t_frame_end - t_frame_start,
        frame_interval_ms: prev_start ? (t_frame_start - prev_start) : (t_frame_end - t_frame_start),
        // legacy alias (compat ancien analyze.py) — = frame_interval_ms
        frame_time_ms:     prev_start ? (t_frame_start - prev_start) : (t_frame_end - t_frame_start),
        visible:           visible,
      });
      benchState.last_frame_start_ms = t_frame_start;
      benchState.last_frame_ms       = t_frame_end;

      // Auto-stop apres duration
      if (elapsed_s >= benchState.duration_s) {
        finishBench();
      }
    }

    return result;
  };

  // --- Bench main flow : fetch scenario, then trigger play, wait duration, post log ---
  async function runBench() {
    benchState.fetch_start_ms = performance.now();
    let resp, raw;
    try {
      resp = await fetch(`/api/bench/scenario/${benchLimit}`);
      raw = await resp.text();
    } catch (e) {
      console.error("[bench] fetch failed", e);
      window.BENCH_RESULT = { ok: false, error: String(e) };
      return;
    }
    benchState.fetch_end_ms = performance.now();
    benchState.payload_bytes = raw.length;
    const data = JSON.parse(raw);
    benchState.parse_end_ms = performance.now();
    benchState.n_trips = (data.trips || []).length;

    console.log(`[bench] scenario fetched: ${benchState.n_trips} trips, ` +
                `${benchState.payload_bytes} bytes, ` +
                `fetch=${(benchState.fetch_end_ms-benchState.fetch_start_ms).toFixed(0)}ms ` +
                `parse=${(benchState.parse_end_ms-benchState.fetch_end_ms).toFixed(0)}ms`);

    // Inject les trips dans stibTp et demarrer le player
    if (typeof stibTp === "undefined" || typeof tpStartPlayer !== "function") {
      console.error("[bench] stibTp / tpStartPlayer introuvables");
      window.BENCH_RESULT = { ok: false, error: "stibTp not found" };
      return;
    }
    stibTp.trajectories = data.trips || [];
    if (typeof tpComputeTripLabels === "function") {
      stibTp.tripLabelMap = tpComputeTripLabels(stibTp.trajectories);
    }
    // Calcule windowStart/End comme le code original
    const allMs = [];
    for (const t of stibTp.trajectories) {
      const s = t.samples;
      if (s && s.length) {
        allMs.push(s[0][0], s[s.length - 1][0]);
      }
    }
    if (allMs.length) {
      stibTp.windowStartMs = Math.min(...allMs);
      stibTp.windowEndMs   = Math.max(...allMs);
      stibTp.playheadMs    = stibTp.windowStartMs;
    }
    if (typeof tpEnsureShapesLoaded === "function") {
      try { await tpEnsureShapesLoaded(stibTp.trajectories); } catch (e) { /* ignore */ }
    }

    benchState.play_start_ms = performance.now();
    if (typeof tpStartPlayer === "function") {
      try { tpStartPlayer(); } catch (e) { console.error("[bench] tpStartPlayer error", e); }
    }
    console.log(`[bench] player started, capturing ${benchState.duration_s}s`);
  }

  async function finishBench() {
    if (benchState.done) return;
    benchState.done = true;

    // Stop player
    try {
      if (typeof stibTp !== "undefined" && stibTp.rafId) {
        cancelAnimationFrame(stibTp.rafId);
        stibTp.playing = false;
      }
    } catch (e) { /* ignore */ }

    const payload = {
      limit:  benchState.limit,
      run_id: benchState.run_id,
      load: {
        fetch_start_ms: benchState.fetch_start_ms,
        fetch_end_ms:   benchState.fetch_end_ms,
        parse_end_ms:   benchState.parse_end_ms,
        first_frame_ms: benchState.first_frame_ms,
        payload_bytes:  benchState.payload_bytes,
        n_trips:        benchState.n_trips,
      },
      frames: benchState.frames,
    };

    try {
      const resp = await fetch("/api/bench/log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await resp.json();
      console.log("[bench] done", result);
      window.BENCH_RESULT = { ok: true, ...result, n_frames: benchState.frames.length };
    } catch (e) {
      console.error("[bench] log post failed", e);
      window.BENCH_RESULT = { ok: false, error: String(e) };
    }
  }

  // --- Entry point : declenche apres l'init de la page ---
  if (document.readyState === "complete") {
    setTimeout(runBench, 100);
  } else {
    window.addEventListener("load", () => setTimeout(runBench, 100));
  }
})();
