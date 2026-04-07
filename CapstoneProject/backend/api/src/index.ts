import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

import express from "express";
import multer from "multer";

type Detection = {
  class_id: number;
  label: string;
  confidence: number;
  xyxy: number[] | null;
};

type GunDetectJson = {
  image_path: string;
  weights_path: string;
  confidence_threshold: number;
  has_gun: boolean;
  detections: Detection[];
  annotated_path: string | null;
};

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const repoRoot = path.resolve(__dirname, "..", "..", "..");
const backendRoot = path.join(repoRoot, "backend");
const scriptPath = path.join(backendRoot, "scripts", "gunDetect.py");

const defaultWeightsPath = path.join(backendRoot, "models", "weapon_best.pt");
const weightsPath = process.env.WEIGHTS_PATH || defaultWeightsPath;
const pythonBinEnv = process.env.PYTHON_BIN;

const port = Number(process.env.PORT || 5175);

const storageRoot = path.join(backendRoot, "api_storage");
const uploadsDir = path.join(storageRoot, "uploads");
const annotatedDir = path.join(storageRoot, "annotated");

fs.mkdirSync(uploadsDir, { recursive: true });
fs.mkdirSync(annotatedDir, { recursive: true });

const upload = multer({ dest: uploadsDir });
const app = express();
app.use(express.json({ limit: "2mb" }));
app.use("/files", express.static(storageRoot));

function run(cmd: string, args: string[]): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString("utf8")));
    child.stderr.on("data", (d) => (stderr += d.toString("utf8")));
    child.on("error", reject);
    child.on("close", (code) => resolve({ stdout, stderr, code: code ?? -1 }));
  });
}

async function detectWithPython(params: {
  imagePath: string;
  conf: number;
  saveAnnotated: boolean;
}): Promise<GunDetectJson> {
  const requestId = crypto.randomUUID();
  const annotatedPath = params.saveAnnotated ? path.join(annotatedDir, `${requestId}.jpg`) : null;

  const baseArgs = [
    scriptPath,
    params.imagePath,
    "--weights",
    weightsPath,
    "--conf",
    String(params.conf),
    "--json",
  ];

  if (annotatedPath) {
    baseArgs.push("--save", annotatedPath);
  }

  const pythonCandidates = pythonBinEnv ? [pythonBinEnv] : os.platform() === "win32" ? ["python", "python3"] : ["python3", "python"];
  let lastError: unknown = null;

  for (const pythonCmd of pythonCandidates) {
    try {
      const { stdout, stderr, code } = await run(pythonCmd, baseArgs);
      if (code !== 0 && stdout.trim().length === 0) {
        throw new Error(`gunDetect failed (exit ${code}). stderr: ${stderr}`);
      }

      const jsonLine = stdout.trim().split(/\r?\n/).pop() ?? "";
      const parsed = JSON.parse(jsonLine) as GunDetectJson;
      return parsed;
    } catch (err) {
      lastError = err;
    }
  }

  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

app.get("/api/health", (_req, res) => {
  res.json({
    ok: true,
    scriptPath,
    weightsPath,
  });
});

app.post("/api/detect-path", async (req, res) => {
  const imagePath = String(req.body?.imagePath || "");
  const conf = Number(req.body?.conf ?? 0.4);
  const saveAnnotated = Boolean(req.body?.saveAnnotated ?? true);

  if (!imagePath) return res.status(400).json({ error: "Missing imagePath" });
  if (!Number.isFinite(conf) || conf <= 0 || conf > 1) return res.status(400).json({ error: "conf must be in (0, 1]" });

  try {
    const result = await detectWithPython({ imagePath, conf, saveAnnotated });
    const annotatedUrl = result.annotated_path ? `/files/annotated/${path.basename(result.annotated_path)}` : null;
    res.json({ ...result, annotated_url: annotatedUrl });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.post("/api/detect-upload", upload.single("image"), async (req, res) => {
  const conf = Number(req.body?.conf ?? 0.4);
  const saveAnnotated = Boolean((req.body?.saveAnnotated ?? "true") === "true");

  if (!req.file) return res.status(400).json({ error: "Missing image file field 'image'" });
  if (!Number.isFinite(conf) || conf <= 0 || conf > 1) return res.status(400).json({ error: "conf must be in (0, 1]" });

  const ext = path.extname(req.file.originalname) || ".jpg";
  const renamed = `${req.file.filename}${ext}`;
  const finalPath = path.join(uploadsDir, renamed);
  fs.renameSync(req.file.path, finalPath);

  try {
    const result = await detectWithPython({ imagePath: finalPath, conf, saveAnnotated });
    const uploadedUrl = `/files/uploads/${renamed}`;
    const annotatedUrl = result.annotated_path ? `/files/annotated/${path.basename(result.annotated_path)}` : null;
    res.json({ ...result, uploaded_url: uploadedUrl, annotated_url: annotatedUrl });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.listen(port, () => {
  // eslint-disable-next-line no-console
  console.log(`GunDetect API listening on http://localhost:${port}`);
});
