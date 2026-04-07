import { useEffect, useMemo, useRef, useState } from "react";
import { detectPath, detectUpload, fetchIrDetection, listImages, poiMatchBase64, poiMatchUpload, type DetectResponse, type ImageListItem, type POIMatchResponse } from "./api";

function fmtPct(x: number) {
  if (!Number.isFinite(x)) return "–";
  return `${(x * 100).toFixed(1)}%`;
}

export default function App() {
  const [mode, setMode] = useState<"upload" | "path">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [serverPath, setServerPath] = useState("");
  const [conf, setConf] = useState(0.4);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DetectResponse | null>(null);
  const [gallery, setGallery] = useState<ImageListItem[]>([]);
  const [query, setQuery] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const [backendLogs, setBackendLogs] = useState<string[] | null>(null);
  const [poiFile, setPoiFile] = useState<File | null>(null);
  const [poiLoading, setPoiLoading] = useState(false);
  const [poiError, setPoiError] = useState<string | null>(null);
  const [poiResult, setPoiResult] = useState<POIMatchResponse | null>(null);
  const [poiCaptureUrl, setPoiCaptureUrl] = useState<string | null>(null);
  const defaultCameraStreamUrl = import.meta.env.VITE_CAMERA_STREAM_URL ?? "/api/camera/stream";
  const defaultCameraFrameUrl = import.meta.env.VITE_CAMERA_FRAME_URL ?? "/api/camera/frame";
  const defaultIrStreamUrl = import.meta.env.VITE_IR_CAMERA_STREAM_URL ?? "/api/ir/stream";
  const defaultIrFrameUrl = import.meta.env.VITE_IR_CAMERA_FRAME_URL ?? "/api/ir/frame";
  const [cameraStreamUrl, setCameraStreamUrl] = useState<string>(() => {
    const saved = localStorage.getItem("cameraStreamUrl");
    return saved ?? defaultCameraStreamUrl;
  });
  const [cameraFrameUrl, setCameraFrameUrl] = useState<string>(() => `${defaultCameraFrameUrl}?ts=${Date.now()}`);
  const [usePolling, setUsePolling] = useState(false);
  const [cameraStatus, setCameraStatus] = useState<"idle" | "loading" | "live" | "captured" | "error">("idle");
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [irStreamUrl, setIrStreamUrl] = useState<string>(() => {
    const saved = localStorage.getItem("irStreamUrl");
    return saved ?? defaultIrStreamUrl;
  });
  const [irFrameUrl, setIrFrameUrl] = useState<string>(() => `${defaultIrFrameUrl}?ts=${Date.now()}`);
  const [useIrPolling, setUseIrPolling] = useState(false);
  const [irStatus, setIrStatus] = useState<"idle" | "loading" | "live" | "error">("idle");
  const [irError, setIrError] = useState<string | null>(null);
  const [irResult, setIrResult] = useState<DetectResponse | null>(null);
  const [showPoiUpload, setShowPoiUpload] = useState(false);

  const cameraImgRef = useRef<HTMLImageElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const autoCaptureTimerRef = useRef<number | null>(null);
  const pollTimerRef = useRef<number | null>(null);
  const irPollTimerRef = useRef<number | null>(null);
  const irDetectTimerRef = useRef<number | null>(null);
  const irDetectBusyRef = useRef(false);
  const showPoiUploadRef = useRef(showPoiUpload);

  function log(message: string) {
    const ts = new Date().toLocaleTimeString();
    setLogs((prev) => [`[${ts}] ${message}`, ...prev].slice(0, 200));
  }

  const previewUrl = useMemo(() => {
    if (!file) return null;
    return URL.createObjectURL(file);
  }, [file]);

  const serverPreviewUrl = useMemo(() => {
    if (!serverPath.trim()) return null;
    return `/api/image?path=${encodeURIComponent(serverPath.trim())}`;
  }, [serverPath]);

  const annotatedUrl = useMemo(() => {
    if (!result?.annotated_png_base64) return null;
    return `data:image/png;base64,${result.annotated_png_base64}`;
  }, [result]);
  const irAnnotatedUrl = useMemo(() => {
    if (!irResult?.annotated_png_base64) return null;
    return `data:image/png;base64,${irResult.annotated_png_base64}`;
  }, [irResult]);

  const poiPreviewUrl = useMemo(() => {
    if (poiCaptureUrl) return poiCaptureUrl;
    if (!poiFile) return null;
    return URL.createObjectURL(poiFile);
  }, [poiCaptureUrl, poiFile]);
  const poiDetails = poiResult?.poi_details ?? null;
  const poiWanted = poiResult?.match && poiDetails?.wanted === true;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await listImages("repo");
        if (cancelled) return;
        setGallery(res.items);
        log(`Loaded gallery: ${res.count} images`);
      } catch (e) {
        if (cancelled) return;
        setGallery([]);
        log(`Gallery load failed: ${e instanceof Error ? e.message : String(e)}`);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    localStorage.setItem("cameraStreamUrl", cameraStreamUrl);
  }, [cameraStreamUrl]);

  useEffect(() => {
    localStorage.setItem("irStreamUrl", irStreamUrl);
  }, [irStreamUrl]);

  useEffect(() => {
    if (pollTimerRef.current) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (!usePolling) return;

    const tick = () => {
      setCameraFrameUrl(`${defaultCameraFrameUrl}?ts=${Date.now()}`);
    };
    tick();
    pollTimerRef.current = window.setInterval(tick, 200);

    return () => {
      if (pollTimerRef.current) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [usePolling, defaultCameraFrameUrl]);

  useEffect(() => {
    if (irPollTimerRef.current) {
      window.clearInterval(irPollTimerRef.current);
      irPollTimerRef.current = null;
    }
    if (!useIrPolling) return;

    const tick = () => {
      setIrFrameUrl(`${defaultIrFrameUrl}?ts=${Date.now()}`);
    };
    tick();
    irPollTimerRef.current = window.setInterval(tick, 250);

    return () => {
      if (irPollTimerRef.current) {
        window.clearInterval(irPollTimerRef.current);
        irPollTimerRef.current = null;
      }
    };
  }, [useIrPolling, defaultIrFrameUrl]);

  const filteredGallery = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return gallery;
    return gallery.filter((it) => it.path.toLowerCase().includes(q) || it.name.toLowerCase().includes(q));
  }, [gallery, query]);

  async function onDetect() {
    if (mode === "upload" && !file) return;
    if (mode === "path" && !serverPath.trim()) return;
    setLoading(true);
    setError(null);
    try {
      log(`Detect start: mode=${mode}, conf=${conf}`);
      if (mode === "upload") {
        log(`Upload file: ${file?.name ?? "unknown"}`);
      } else {
        log(`Server path: ${serverPath.trim()}`);
      }
      const res = mode === "upload" ? await detectUpload(file!, conf) : await detectPath(serverPath.trim(), conf);
      setResult(res);
      log(`Detect done: has_gun=${res.has_gun}, detections=${res.detections.length}`);
      if (res.warning) {
        log(`Warning: ${res.warning}`);
      }
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : String(e));
      log(`Detect error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  }

  async function runPoiMatchUpload(file: File) {
    setPoiLoading(true);
    setPoiError(null);
    try {
      log(`POI match start: file=${file.name}`);
      const res = await poiMatchUpload(file);
      setPoiResult(res);
      log(`POI match done: match=${res.match}, distance=${res.distance.toFixed(4)}`);
    } catch (e) {
      setPoiResult(null);
      setPoiError(e instanceof Error ? e.message : String(e));
      log(`POI match error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setPoiLoading(false);
    }
  }

  async function runPoiMatchBase64(dataUrl: string) {
    setPoiLoading(true);
    setPoiError(null);
    try {
      log("POI match start: camera capture");
      const res = await poiMatchBase64(dataUrl, "capture.jpg");
      setPoiResult(res);
      log(`POI match done: match=${res.match}, distance=${res.distance.toFixed(4)}`);
    } catch (e) {
      setPoiResult(null);
      setPoiError(e instanceof Error ? e.message : String(e));
      log(`POI match error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setPoiLoading(false);
    }
  }

  async function onPoiMatch() {
    if (!poiFile) return;
    setPoiCaptureUrl(null);
    await runPoiMatchUpload(poiFile);
  }

  async function refreshIrDetection() {
    if (irDetectBusyRef.current) return;
    irDetectBusyRef.current = true;
    try {
      const res = await fetchIrDetection(conf);
      setIrResult(res);
      setIrError(null);
    } catch (e) {
      setIrError(e instanceof Error ? e.message : String(e));
    } finally {
      irDetectBusyRef.current = false;
    }
  }

  function scheduleAutoCapture(delayMs: number = 1500) {
    if (autoCaptureTimerRef.current) {
      window.clearTimeout(autoCaptureTimerRef.current);
    }
    autoCaptureTimerRef.current = window.setTimeout(() => {
      void captureAndMatch();
    }, delayMs);
  }

  function captureFrame(): string | null {
    const img = cameraImgRef.current;
    const canvas = canvasRef.current;
    if (!img || !canvas) return null;
    const width = img.naturalWidth || 0;
    const height = img.naturalHeight || 0;
    if (!width || !height) return null;
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(img, 0, 0, width, height);
    return canvas.toDataURL("image/jpeg", 0.92);
  }

  async function captureAndMatch() {
    if (showPoiUploadRef.current) return;
    if (poiLoading) return;
    const dataUrl = captureFrame();
    if (!dataUrl) {
      log("Camera not ready; retrying capture.");
      scheduleAutoCapture(500);
      return;
    }
    setPoiCaptureUrl(dataUrl);
    setPoiFile(null);
    setPoiResult(null);
    setPoiError(null);
    setCameraStatus("captured");
    log("Captured photo from camera");
    await runPoiMatchBase64(dataUrl);
  }

  useEffect(() => {
    if (autoCaptureTimerRef.current) {
      window.clearTimeout(autoCaptureTimerRef.current);
      autoCaptureTimerRef.current = null;
    }
    if (pollTimerRef.current) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setUsePolling(false);
    if (!cameraStreamUrl.trim()) {
      setCameraStatus("error");
      setCameraError("Camera stream URL is empty.");
      return;
    }
    setCameraStatus("loading");
    setCameraError(null);
  }, [cameraStreamUrl]);

  useEffect(() => {
    if (irPollTimerRef.current) {
      window.clearInterval(irPollTimerRef.current);
      irPollTimerRef.current = null;
    }
    if (irDetectTimerRef.current) {
      window.clearInterval(irDetectTimerRef.current);
      irDetectTimerRef.current = null;
    }
    setUseIrPolling(false);
    if (!irStreamUrl.trim()) {
      setIrStatus("error");
      setIrError("IR camera stream URL is empty.");
      return;
    }
    setIrStatus("loading");
    setIrError(null);
  }, [irStreamUrl]);

  useEffect(() => {
    if (irDetectTimerRef.current) {
      window.clearInterval(irDetectTimerRef.current);
      irDetectTimerRef.current = null;
    }
    if (irStatus !== "live") return;
    void refreshIrDetection();
    irDetectTimerRef.current = window.setInterval(() => {
      void refreshIrDetection();
    }, 2500);
    return () => {
      if (irDetectTimerRef.current) {
        window.clearInterval(irDetectTimerRef.current);
        irDetectTimerRef.current = null;
      }
    };
  }, [irStatus, conf]);

  useEffect(() => {
    if (cameraStatus === "error") {
      setShowPoiUpload(true);
    }
  }, [cameraStatus]);

  useEffect(() => {
    showPoiUploadRef.current = showPoiUpload;
    if (showPoiUpload && autoCaptureTimerRef.current) {
      window.clearTimeout(autoCaptureTimerRef.current);
      autoCaptureTimerRef.current = null;
    }
  }, [showPoiUpload]);

  async function loadBackendLogs() {
    try {
      const res = await fetch("/api/logs?tail=200");
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Request failed (${res.status})`);
      }
      const data = (await res.json()) as { lines?: string[] };
      setBackendLogs(data.lines ?? []);
      log("Loaded backend logs");
    } catch (e) {
      log(`Backend logs error: ${e instanceof Error ? e.message : String(e)}`);
      setBackendLogs(null);
    }
  }

  const status = result?.has_gun ? "Gun detected" : result ? "No gun detected" : "—";
  const statusTone = result?.has_gun ? "danger" : result ? "ok" : "neutral";
  const irStatusLabel = irResult?.has_knife
    ? "Knife detected in IR feed"
    : irResult?.has_weapon
      ? "Weapon detected in IR feed"
      : irResult
        ? "No weapon detected in IR feed"
        : "IR monitoring idle";
  const irStatusTone = irResult?.has_weapon ? "danger" : irResult ? "ok" : "neutral";

  return (
    <div className="page">
      <header className="header">
        <div>
          <div className="title">Weapon Detection</div>
          <div className="subtitle">YOLOv8 • `weapon_best.pt`</div>
        </div>
        <div className="headerLinks">
          <a className="link" href="/api/health" target="_blank" rel="noreferrer">
            Backend health
          </a>
          <a className="link" href="/api/logs" target="_blank" rel="noreferrer">
            Backend logs
          </a>
        </div>
      </header>

      <main className="grid">
        <section className="card">
          <div className="cardTitle">Input</div>

          <div className="row">
            <div className="tabs">
              <button
                className={`tab ${mode === "upload" ? "active" : ""}`}
                onClick={() => {
                  setMode("upload");
                  setResult(null);
                  setError(null);
                  log("Mode set to upload");
                }}
                type="button"
              >
                Upload
              </button>
              <button
                className={`tab ${mode === "path" ? "active" : ""}`}
                onClick={() => {
                  setMode("path");
                  setResult(null);
                  setError(null);
                  log("Mode set to server path");
                }}
                type="button"
              >
                Server path
              </button>
            </div>
            {mode === "path" ? (
              <div className="hint">
                Path must exist on the machine running the backend (Windows example: <span className="mono">C:\images\test.jpg</span>).
              </div>
            ) : null}
          </div>

          <div className="row">
            {mode === "upload" ? (
              <label className="label">
                Image
                <input
                  key="upload-input"
                  className="input"
                  type="file"
                  accept="image/*"
                  onChange={(e) => {
                    const f = e.target.files?.[0] ?? null;
                    setFile(f);
                    setResult(null);
                    setError(null);
                    if (f) log(`Selected file: ${f.name}`);
                  }}
                />
              </label>
            ) : (
              <label className="label">
                Image path (backend machine)
                <input
                  key="path-input"
                  className="input"
                  placeholder="e.g. C:\\images\\test.jpg or /Users/me/test.jpg"
                  value={serverPath}
                  onChange={(e) => setServerPath(e.target.value)}
                />
              </label>
            )}
          </div>

          <div className="row split">
            <label className="label">
              Confidence threshold
              <input
                className="input"
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={conf}
                onChange={(e) => setConf(Number(e.target.value))}
              />
            </label>

            <button
              className="button"
              disabled={(mode === "upload" ? !file : !serverPath.trim()) || loading}
              onClick={onDetect}
            >
              {loading ? "Detecting…" : "Run detection"}
            </button>
          </div>

          {error ? <div className="error">{error}</div> : null}

          <div className="imageFrame">
            {mode === "upload" ? (
              previewUrl ? (
                <img className="image" src={previewUrl} alt="Selected" />
              ) : (
                <div className="placeholder">Select an image to preview</div>
              )
            ) : (
              serverPreviewUrl ? (
                <img className="image" src={serverPreviewUrl} alt="Selected (server)" />
              ) : (
                <div className="placeholder">Enter a server path or choose from the gallery</div>
              )
            )}
          </div>

          <div className="row" style={{ marginTop: 12 }}>
            <div className="cardTitle" style={{ marginBottom: 8 }}>
              Project images
            </div>
            <label className="label">
              Search
              <input className="input" placeholder="e.g. sample_images, .jpg, datasets" value={query} onChange={(e) => setQuery(e.target.value)} />
            </label>
            <div className="thumbGrid">
              {filteredGallery.slice(0, 120).map((it) => (
                <button
                  key={it.path}
                  className={`thumb ${serverPath.trim() === it.path ? "active" : ""}`}
                  type="button"
                  onClick={() => {
                    setMode("path");
                    setServerPath(it.path);
                    setFile(null);
                    setResult(null);
                    setError(null);
                    log(`Selected gallery image: ${it.path}`);
                  }}
                  title={it.path}
                >
                  {it.thumb_jpeg_base64 ? (
                    <img className="thumbImg" src={`data:image/jpeg;base64,${it.thumb_jpeg_base64}`} alt={it.name} />
                  ) : (
                    <div className="thumbFallback">{it.name}</div>
                  )}
                </button>
              ))}
            </div>
            <div className="hint" style={{ marginTop: 8 }}>
              Showing {Math.min(filteredGallery.length, 120)} of {filteredGallery.length} images found.
            </div>
          </div>
        </section>

        <section className="card">
          <div className="cardTitle">Result</div>

          <div className={`status ${statusTone}`}>
            <div className="statusLabel">{status}</div>
            {result ? <div className="statusMeta">Detections: {result.detections.length}</div> : <div className="statusMeta">Upload an image and run detection</div>}
          </div>

          {result ? (
            <>
              {result.warning ? <div className="error">{result.warning}</div> : null}
              <div className="smallMeta">
                Model: <span className="mono">{result.weights_path}</span>
              </div>
              {result.debug ? (
                <div className="smallMeta">
                  Used conf/imgsz/augment: <span className="mono">{result.debug.used_conf ?? "—"}</span> /{" "}
                  <span className="mono">{result.debug.used_imgsz ?? "—"}</span> /{" "}
                  <span className="mono">{String(result.debug.used_augment ?? "—")}</span>
                </div>
              ) : null}

              <div className="table">
                <div className="thead">
                  <div>Label</div>
                  <div>Confidence</div>
                </div>
                {result.detections.length ? (
                  result.detections
                    .slice()
                    .sort((a, b) => b.confidence - a.confidence)
                    .map((d, idx) => (
                      <div className="trow" key={`${d.label}-${idx}`}>
                        <div className="pill">{d.label}</div>
                        <div className="mono">{fmtPct(d.confidence)}</div>
                      </div>
                    ))
                ) : (
                  <div className="trow muted">
                    <div>No detections above threshold</div>
                    <div className="mono">{fmtPct(result.confidence_threshold)}</div>
                  </div>
                )}
              </div>

              <div className="imageFrame">
                {annotatedUrl ? <img className="image" src={annotatedUrl} alt="Annotated result" /> : <div className="placeholder">Annotated preview unavailable</div>}
              </div>
            </>
          ) : null}

          <div className="cardTitle" style={{ marginTop: 14 }}>
            Frontend logs
          </div>
          <div className="logPanel">
            {logs.length ? logs.map((line, idx) => <div key={idx} className="logLine">{line}</div>) : <div className="logLine muted">No logs yet.</div>}
          </div>
          <div className="logActions">
            <button className="button subtle" type="button" onClick={() => setLogs([])}>
              Clear
            </button>
            <button className="button subtle" type="button" onClick={loadBackendLogs}>
              Fetch backend logs
            </button>
          </div>
          {backendLogs ? (
            <>
              <div className="cardTitle" style={{ marginTop: 10 }}>
                Backend logs (tail)
              </div>
              <div className="logPanel">
                {backendLogs.length ? backendLogs.map((line, idx) => <div key={idx} className="logLine">{line}</div>) : <div className="logLine muted">No backend logs.</div>}
              </div>
            </>
          ) : null}
        </section>
      </main>

      <section className={`card ${poiWanted ? "wanted" : ""}`} style={{ marginTop: 14 }}>
        <div className="cardTitle">POI Match (Criminal Background Check)</div>
        <div className="row split">
          <div className="hint">
            RGB status: <span className="mono">{cameraStatus}</span>.{" "}
            {cameraStatus === "loading"
              ? "Connecting to camera stream…"
              : cameraStatus === "live"
                ? "Camera live. Auto-capturing shortly."
                : cameraStatus === "captured"
                  ? "Captured. Retake if needed."
                  : cameraStatus === "error"
                    ? "Camera unavailable."
                    : "Camera idle."}
            {" "}IR status: <span className="mono">{irStatus}</span>.
          </div>
          <div className="logActions" style={{ marginTop: 0 }}>
            <button
              className="button subtle"
              type="button"
              disabled={cameraStatus === "loading" || cameraStatus === "error" || poiLoading}
              onClick={() => {
                if (cameraStatus === "loading" || cameraStatus === "error") return;
                setPoiCaptureUrl(null);
                setPoiResult(null);
                setPoiError(null);
                setCameraStatus("live");
                scheduleAutoCapture(300);
              }}
            >
              {poiLoading ? "Matching…" : "Retake photo"}
            </button>
            <button className="button subtle" type="button" onClick={() => setShowPoiUpload((v) => !v)}>
              {showPoiUpload ? "Hide upload" : "Use file upload"}
            </button>
          </div>
        </div>

        <div className="row">
          <label className="label">
            RGB camera stream URL (Pi)
            <input
              className="input"
              placeholder="http://pi.local:9000/api/camera/stream"
              value={cameraStreamUrl}
              onChange={(e) => setCameraStreamUrl(e.target.value)}
            />
          </label>
        </div>

        <div className="row">
          <label className="label">
            IR camera stream URL (Pi)
            <input
              className="input"
              placeholder="http://pi.local:9000/api/ir/stream"
              value={irStreamUrl}
              onChange={(e) => setIrStreamUrl(e.target.value)}
            />
          </label>
        </div>

        {cameraError ? <div className="error">{cameraError}</div> : null}
        {irError ? <div className="error">{irError}</div> : null}

        {showPoiUpload ? (
          <div className="row split">
            <label className="label">
              Suspect image (upload)
              <input
                className="input"
                type="file"
                accept="image/*"
                onChange={(e) => {
                  const f = e.target.files?.[0] ?? null;
                  setPoiFile(f);
                  setPoiCaptureUrl(null);
                  setPoiResult(null);
                  setPoiError(null);
                  if (f) log(`POI file selected: ${f.name}`);
                }}
              />
            </label>
            <button className="button" disabled={!poiFile || poiLoading} onClick={onPoiMatch}>
              {poiLoading ? "Matching…" : "Run POI match"}
            </button>
          </div>
        ) : (
          <div className="row">
            <div className="hint">The Pi camera stream auto-loads and captures a photo for matching.</div>
          </div>
        )}

        <div className="row">
          <div className="hint">
            Uses POI embeddings from <span className="mono">backend/data/poi_db/poi_embeddings.json</span>.
          </div>
        </div>
        <div className="row">
          <div className="hint">
            IR alerting uses the current YOLO weapon weights. The existing model only guarantees the class it was trained on; knife-specific alerts require knife labels in the weights.
          </div>
        </div>
        {poiError ? <div className="error">{poiError}</div> : null}
        <div className="poiGrid">
          <div className="imageFrame">
            <img
              key={usePolling ? cameraFrameUrl : cameraStreamUrl}
              ref={cameraImgRef}
              className="image"
              src={usePolling ? cameraFrameUrl : cameraStreamUrl}
              crossOrigin="anonymous"
              onLoad={() => {
                if (cameraStatus !== "live") {
                  setCameraStatus("live");
                  setCameraError(null);
                  log(usePolling ? "Camera polling live" : "Camera stream live");
                  scheduleAutoCapture();
                }
              }}
              onError={() => {
                if (!usePolling) {
                  setUsePolling(true);
                  setCameraStatus("loading");
                  const message = "Stream failed; falling back to polling.";
                  setCameraError(message);
                  log(`Camera stream error: ${message}`);
                  return;
                }
                setCameraStatus("error");
                const message = "Unable to load camera frame.";
                setCameraError(message);
                log(`Camera frame error: ${message}`);
              }}
              style={{ display: poiPreviewUrl ? "none" : "block" }}
            />
            {poiPreviewUrl ? <img className="image" src={poiPreviewUrl} alt="Suspect" /> : null}
            {!poiPreviewUrl && cameraStatus !== "live" ? (
              <div className="placeholder">
                {cameraStatus === "loading"
                  ? "Connecting to camera…"
                  : cameraStatus === "error"
                    ? "Camera unavailable"
                    : "Camera idle"}
              </div>
            ) : null}
            <canvas ref={canvasRef} style={{ display: "none" }} />
          </div>
          <div className="imageFrame">
            <img
              key={useIrPolling ? irFrameUrl : irStreamUrl}
              className="image"
              src={useIrPolling ? irFrameUrl : irStreamUrl}
              onLoad={() => {
                if (irStatus !== "live") {
                  setIrStatus("live");
                  setIrError(null);
                }
              }}
              onError={() => {
                if (!useIrPolling) {
                  setUseIrPolling(true);
                  setIrStatus("loading");
                  setIrError("IR stream failed; falling back to polling.");
                  return;
                }
                setIrStatus("error");
                setIrError("Unable to load IR frame.");
              }}
            />
            {irStatus !== "live" ? (
              <div className="placeholder">
                {irStatus === "loading" ? "Connecting to IR camera…" : irStatus === "error" ? "IR camera unavailable" : "IR camera idle"}
              </div>
            ) : null}
          </div>
        </div>
        <div className={`status ${irStatusTone}`} style={{ marginTop: 10 }}>
          <div className="statusLabel">{irStatusLabel}</div>
          <div className="statusMeta">
            {irResult
              ? `Labels: ${irResult.detections.length ? irResult.detections.map((d) => d.label).join(", ") : "none"}`
              : irStatus === "live"
                ? "IR feed live. Waiting for detection results."
                : "IR feed not live yet."}
          </div>
        </div>
        {(irAnnotatedUrl || poiResult?.poi_image_path) ? (
          <div className="poiGrid" style={{ marginTop: 12 }}>
            <div className="imageFrame">
              {irAnnotatedUrl ? (
                <img className="image" src={irAnnotatedUrl} alt="Annotated IR result" />
              ) : (
                <div className="placeholder">IR detection snapshot will appear here</div>
              )}
            </div>
            <div className="imageFrame">
              {poiResult?.poi_image_path ? (
                <img className="image" src={`/api/image?path=${encodeURIComponent(poiResult.poi_image_path)}`} alt="Matched POI" />
              ) : (
                <div className="placeholder">Matched POI image will appear here</div>
              )}
            </div>
          </div>
        ) : null}
        {poiResult ? (
          <div className={`status ${poiResult.match ? "danger" : "ok"}`} style={{ marginTop: 10 }}>
            <div className="statusLabel">
              {poiResult.match ? "Criminal background detected" : "No criminal match found"}
            </div>
            <div className="statusMeta">
              Best match: {poiResult.poi_name ?? "—"} • Distance: {poiResult.distance.toFixed(4)} • Threshold: {poiResult.threshold.toFixed(4)}
            </div>
          </div>
        ) : null}
        {poiResult && poiResult.match && poiDetails ? (
          <div className={`poiDetails ${poiWanted ? "wanted" : ""}`}>
            <div className="poiDetailsTitle">Matched Record</div>
            <div className="poiDetailsRow">Name: {poiDetails.name ?? poiResult.poi_name ?? "—"}</div>
            {poiDetails.age != null ? <div className="poiDetailsRow">Age: {poiDetails.age}</div> : null}
            {poiDetails.dob ? <div className="poiDetailsRow">DOB: {poiDetails.dob}</div> : null}
            {poiDetails.crime ? <div className="poiDetailsRow">Crime Committed: {poiDetails.crime}</div> : null}
            {poiDetails.extra_info ? <div className="poiDetailsRow">Additional Info: {poiDetails.extra_info}</div> : null}
            {poiDetails.wanted === true ? (
              <div className="poiDetailsRow">Currently wanted</div>
            ) : poiDetails.wanted === false ? (
              <div className="poiDetailsRow">Not currently wanted</div>
            ) : null}
          </div>
        ) : null}
      </section>

      <footer className="footer">
        Runs locally on macOS/Windows: start backend on <span className="mono">:8000</span> and frontend on <span className="mono">:5173</span>.
      </footer>
    </div>
  );
}
