export type Detection = {
  class_id: number;
  label: string;
  confidence: number;
  xyxy: number[] | null;
};

export type DetectResponse = {
  has_gun: boolean;
  has_weapon?: boolean;
  has_knife?: boolean;
  confidence_threshold: number;
  weights_path: string;
  detections: Detection[];
  annotated_png_base64: string | null;
  warning?: string | null;
  debug?: {
    used_conf?: number;
    used_imgsz?: number;
    used_augment?: boolean;
  } | null;
};

export type POIMatchResponse = {
  match: boolean;
  distance: number;
  threshold: number;
  model_name: string;
  detector_backend: string;
  distance_metric: string;
  poi_name: string | null;
  poi_image_path: string | null;
  poi_details?: {
    name?: string | null;
    age?: number | null;
    dob?: string | null;
    crime?: string | null;
    wanted?: boolean | null;
    extra_info?: string | null;
  } | null;
};

export type ImageListItem = {
  path: string;
  name: string;
  thumb_jpeg_base64: string | null;
};

export type ImageListResponse = {
  scope: "backend" | "repo";
  count: number;
  items: ImageListItem[];
};

export async function detectUpload(file: File, confThreshold: number): Promise<DetectResponse> {
  const form = new FormData();
  form.append("file", file);

  const url = `/api/detect?conf_threshold=${encodeURIComponent(confThreshold)}`;
  const res = await fetch(url, { method: "POST", body: form });
  if (!res.ok) {
    const text = await res.text();
    try {
      const parsed = JSON.parse(text) as { detail?: string };
      throw new Error(parsed.detail || text || `Request failed (${res.status})`);
    } catch {
      throw new Error(text || `Request failed (${res.status})`);
    }
  }
  return (await res.json()) as DetectResponse;
}

export async function detectPath(path: string, confThreshold: number): Promise<DetectResponse & { image_path?: string }> {
  const res = await fetch(`/api/detect-path`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, conf_threshold: confThreshold }),
  });
  if (!res.ok) {
    const text = await res.text();
    try {
      const parsed = JSON.parse(text) as { detail?: string };
      throw new Error(parsed.detail || text || `Request failed (${res.status})`);
    } catch {
      throw new Error(text || `Request failed (${res.status})`);
    }
  }
  return (await res.json()) as DetectResponse & { image_path?: string };
}

export async function fetchIrDetection(confThreshold: number): Promise<DetectResponse> {
  const res = await fetch(`/api/ir/detect?conf_threshold=${encodeURIComponent(confThreshold)}`);
  if (!res.ok) {
    const text = await res.text();
    try {
      const parsed = JSON.parse(text) as { detail?: string };
      throw new Error(parsed.detail || text || `Request failed (${res.status})`);
    } catch {
      throw new Error(text || `Request failed (${res.status})`);
    }
  }
  return (await res.json()) as DetectResponse;
}

export async function listImages(scope: "backend" | "repo" = "backend"): Promise<ImageListResponse> {
  const res = await fetch(`/api/images?scope=${encodeURIComponent(scope)}&limit=400`);
  if (!res.ok) {
    const text = await res.text();
    try {
      const parsed = JSON.parse(text) as { detail?: string };
      throw new Error(parsed.detail || text || `Request failed (${res.status})`);
    } catch {
      throw new Error(text || `Request failed (${res.status})`);
    }
  }
  return (await res.json()) as ImageListResponse;
}

export async function poiMatchUpload(file: File): Promise<POIMatchResponse> {
  const form = new FormData();
  form.append("file", file);

  const res = await fetch(`/api/poi/match`, { method: "POST", body: form });
  if (!res.ok) {
    const text = await res.text();
    try {
      const parsed = JSON.parse(text) as { detail?: string };
      throw new Error(parsed.detail || text || `Request failed (${res.status})`);
    } catch {
      throw new Error(text || `Request failed (${res.status})`);
    }
  }
  return (await res.json()) as POIMatchResponse;
}

export async function poiMatchBase64(imageBase64: string, filename?: string): Promise<POIMatchResponse> {
  const res = await fetch(`/api/poi/match-base64`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image_base64: imageBase64, filename }),
  });
  if (!res.ok) {
    const text = await res.text();
    try {
      const parsed = JSON.parse(text) as { detail?: string };
      throw new Error(parsed.detail || text || `Request failed (${res.status})`);
    } catch {
      throw new Error(text || `Request failed (${res.status})`);
    }
  }
  return (await res.json()) as POIMatchResponse;
}
