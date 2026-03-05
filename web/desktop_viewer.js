import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DDSLoader } from 'three/addons/loaders/DDSLoader.js';
import { GLTFExporter } from 'three/addons/exporters/GLTFExporter.js';

const manifest = window.__MANIFEST || null;
const statusEl = document.getElementById('status');

function setStatus(msg) { statusEl.textContent = msg; }

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x3f3f3f);

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 4000);
camera.position.set(2.2, 1.4, 3.0);

const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('viewport'), antialias: true });
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.target.set(0, 1, 0);

const hemi = new THREE.HemisphereLight(0xdceeff, 0x2b3a45, 0.85);
scene.add(hemi);
const sun = new THREE.DirectionalLight(0xffffff, 0.95);
sun.position.set(4, 7, 5);
scene.add(sun);

const grid = new THREE.GridHelper(12, 24, 0x5b5b5b, 0x4a4a4a);
scene.add(grid);

const loader = new GLTFLoader();
const texLoader = new THREE.TextureLoader();
const ddsLoader = new DDSLoader();
const clock = new THREE.Clock();

let modelRoot = null;
let mixer = null;
let modelLoadToken = 0;
let liveLinkRevision = -1;
let livePollBusy = false;
let livePollTimer = null;
let liveLinkEnabled = false;
let noLightingApplied = false;

const slots = new Map();
const slotRevisions = new Map();
const materials = [];

const renderState = {
  uniformLighting: true,
  uniformIntensity: 3.0,
  sunIntensity: 0.0,
  ambientIntensity: 0.0,
  sunAzimuthDeg: 45,
  sunElevationDeg: 50,
};

const textureState = {
  diffBrightness: 1.0,
  tileU: 1.0,
  tileV: 1.0,
};

const textureSlots = {
  diff: null,
};

function resize() {
  const c = renderer.domElement;
  const w = c.clientWidth;
  const h = c.clientHeight;
  if (!w || !h) return;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h, false);
}
window.addEventListener('resize', resize);

function frameObject(obj) {
  const box = new THREE.Box3().setFromObject(obj);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const r = Math.max(size.x, size.y, size.z) * 0.75;
  camera.position.copy(center).add(new THREE.Vector3(r * 1.5, r * 0.9, r * 1.8));
  controls.target.copy(center);
  controls.update();
}

function loadGltf(url) {
  return new Promise((resolve, reject) => loader.load(url, resolve, undefined, reject));
}

function disposeHierarchy(root) {
  if (!root) return;
  root.traverse((obj) => {
    if (obj.geometry && typeof obj.geometry.dispose === 'function') {
      obj.geometry.dispose();
    }
  });
}

async function replaceModel(modelRelPath, shouldFrame) {
  const token = ++modelLoadToken;
  const mixerScale = mixer ? Number(mixer.timeScale || 1.0) : 1.0;
  const cacheUrl = `./${modelRelPath}${modelRelPath.includes('?') ? '&' : '?'}_ts=${Date.now()}`;

  setStatus(`Loading model: ${modelRelPath}`);
  const mg = await loadGltf(cacheUrl);
  if (token !== modelLoadToken) return false;

  const nextRoot = mg.scene;
  if (!nextRoot) throw new Error('GLB scene missing');

  nextRoot.traverse((o) => {
    if (o.isMesh) o.frustumCulled = false;
  });

  if (mixer && modelRoot) {
    clearSlotActions();
  }
  if (modelRoot) {
    scene.remove(modelRoot);
    disposeHierarchy(modelRoot);
  }

  modelRoot = nextRoot;
  scene.add(modelRoot);
  collectMaterials(modelRoot);
  // New model instance requires re-applying no-light material override.
  noLightingApplied = false;
  if (shouldFrame) {
    frameObject(modelRoot);
  }

  mixer = new THREE.AnimationMixer(modelRoot);
  mixer.timeScale = mixerScale;
  applySunDirection();
  applyRenderLighting();
  for (const kind of ['diff']) {
    applyTextureMap(kind, textureSlots[kind]);
  }
  applyTextureTuning();
  rebuildSlotActions();
  return true;
}

async function pollLiveLink() {
  if (!liveLinkEnabled) return;
  if (livePollBusy) return;
  livePollBusy = true;
  try {
    const resp = await fetch(`./live_link.json?_ts=${Date.now()}`, { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data || !data.enabled) return;
    const rev = Number(data.revision);
    const model = String(data.model || 'model_live.glb');
    if (!Number.isFinite(rev)) return;
    if (rev <= liveLinkRevision) return;

    liveLinkRevision = rev;
    await replaceModel(`${model}?rev=${rev}`, false);
    setStatus(`Live model updated (rev ${rev})`);
  } catch (_e) {
    // Live link is optional, ignore temporary read/load errors.
  } finally {
    livePollBusy = false;
  }
}

function startLivePolling() {
  if (!liveLinkEnabled) return;
  if (livePollTimer !== null) return;
  livePollTimer = window.setInterval(() => {
    pollLiveLink();
  }, 400);
}

function stopLivePolling() {
  if (livePollTimer === null) return;
  window.clearInterval(livePollTimer);
  livePollTimer = null;
}

function getSlot(slotId) {
  return slots.get(Number(slotId));
}

function bumpSlotRevision(slotId) {
  const id = Number(slotId);
  const next = (slotRevisions.get(id) || 0) + 1;
  slotRevisions.set(id, next);
  return next;
}

function getSlotRevision(slotId) {
  return slotRevisions.get(Number(slotId)) || 0;
}

function getOrCreateSlot(slotId) {
  const id = Number(slotId);
  let slot = slots.get(id);
  if (!slot) {
    slot = { id, weight: 1, loop: true, mode: 'normal', action: null, url: '', label: '', sourceClip: null, filteredClip: null };
    slots.set(id, slot);
  }
  return slot;
}

function trackTargetKey(trackName) {
  const name = String(trackName || '');
  const dot = name.lastIndexOf('.');
  if (dot <= 0) return name;
  return name.slice(0, dot);
}

function trackIsMoving(track) {
  const values = track?.values;
  const itemSize = Number(track?.getValueSize ? track.getValueSize() : 0);
  if (!values || !itemSize || values.length < itemSize * 2) return false;
  const eps = 1e-5;
  for (let i = itemSize; i < values.length; i += itemSize) {
    for (let j = 0; j < itemSize; j += 1) {
      if (Math.abs(values[i + j] - values[j]) > eps) return true;
    }
  }
  return false;
}

function analyzeSlot(slot) {
  const source = slot?.sourceClip;
  if (!source) {
    return { allBones: new Set(), movingBones: new Set(), movingByTrack: new Map() };
  }
  const allBones = new Set();
  const movingBones = new Set();
  const movingByTrack = new Map();
  for (const track of source.tracks) {
    const bone = trackTargetKey(track.name);
    if (!bone) continue;
    allBones.add(bone);
    const moving = trackIsMoving(track);
    movingByTrack.set(track.name, moving);
    if (moving) movingBones.add(bone);
  }
  return { allBones, movingBones, movingByTrack };
}

function buildRuntimeClip(slot, allowedBones, movingByTrack, onlyMoving, additive) {
  if (!slot?.sourceClip) return null;
  const source = slot.sourceClip;
  const tracks = [];
  for (const track of source.tracks) {
    const bone = trackTargetKey(track.name);
    if (!allowedBones.has(bone)) continue;
    if (onlyMoving && !movingByTrack.get(track.name)) continue;
    tracks.push(track.clone());
  }
  if (!tracks.length) return null;
  const clip = new THREE.AnimationClip(`${source.name || 'clip'}__slot_${slot.id}_${slot.mode}`, source.duration, tracks);
  if (additive) THREE.AnimationUtils.makeClipAdditive(clip, 0);
  return clip;
}

function clearSlotActions() {
  if (!mixer || !modelRoot) return;
  for (const slot of slots.values()) {
    if (slot.action) {
      slot.action.stop();
      mixer.uncacheAction(slot.action.getClip(), modelRoot);
      slot.action = null;
    }
    if (slot.filteredClip) {
      mixer.uncacheClip(slot.filteredClip);
      slot.filteredClip = null;
    }
  }
}

function collectUniqueBones(root) {
  const out = [];
  const seen = new Set();
  root.traverse((obj) => {
    if (!obj.isSkinnedMesh || !obj.skeleton) return;
    for (const b of obj.skeleton.bones) {
      if (!b || seen.has(b.uuid)) continue;
      seen.add(b.uuid);
      out.push(b);
    }
  });
  return out;
}

function bakeMixedClip(sampleFps = 60) {
  if (!mixer || !modelRoot) throw new Error('Viewer not ready');
  const active = Array.from(slots.values()).filter((s) => s.action && s.weight > 0.0001);
  if (!active.length) throw new Error('No active slot actions');

  const fps = Math.max(1, Number(sampleFps) || 60);
  const speed = Math.max(0.0001, Number(mixer.timeScale) || 1.0);
  let duration = 0.0;
  for (const s of active) {
    const clip = s.action.getClip();
    const d = Number(clip?.duration || 0);
    duration = Math.max(duration, d / speed);
  }
  if (!(duration > 0)) duration = 1.0;

  const bones = collectUniqueBones(modelRoot);
  if (!bones.length) throw new Error('No skinned bones found in model');

  const state = {
    mixerTime: Number(mixer.time || 0),
    mixerScale: Number(mixer.timeScale || 1),
    actions: active.map((s) => ({
      action: s.action,
      time: Number(s.action.time || 0),
      enabled: !!s.action.enabled,
      paused: !!s.action.paused,
      weight: Number(s.action.getEffectiveWeight() || s.weight || 1),
      loop: s.action.loop,
      clamp: !!s.action.clampWhenFinished,
    })),
  };

  for (const s of active) {
    const a = s.action;
    a.enabled = true;
    a.paused = false;
    a.setEffectiveWeight(s.weight);
    a.loop = s.loop ? THREE.LoopRepeat : THREE.LoopOnce;
    a.clampWhenFinished = !s.loop;
    a.reset().play();
  }

  const frameCount = Math.max(2, Math.ceil(duration * fps) + 1);
  const times = new Array(frameCount);
  const pos = new Map();
  const rot = new Map();
  const scl = new Map();
  for (const b of bones) {
    pos.set(b.name, new Array(frameCount * 3));
    rot.set(b.name, new Array(frameCount * 4));
    scl.set(b.name, new Array(frameCount * 3));
  }

  for (let f = 0; f < frameCount; f += 1) {
    const t = Math.min(duration, f / fps);
    times[f] = t;
    mixer.setTime(t * speed);
    modelRoot.updateMatrixWorld(true);
    for (const b of bones) {
      const p = pos.get(b.name);
      const r = rot.get(b.name);
      const s = scl.get(b.name);
      const i3 = f * 3;
      const i4 = f * 4;
      p[i3] = b.position.x;
      p[i3 + 1] = b.position.y;
      p[i3 + 2] = b.position.z;
      r[i4] = b.quaternion.x;
      r[i4 + 1] = b.quaternion.y;
      r[i4 + 2] = b.quaternion.z;
      r[i4 + 3] = b.quaternion.w;
      s[i3] = b.scale.x;
      s[i3 + 1] = b.scale.y;
      s[i3 + 2] = b.scale.z;
    }
  }

  const tracks = [];
  for (const b of bones) {
    const name = b.name;
    tracks.push(new THREE.VectorKeyframeTrack(`${name}.position`, times, pos.get(name)));
    tracks.push(new THREE.QuaternionKeyframeTrack(`${name}.quaternion`, times, rot.get(name)));
    tracks.push(new THREE.VectorKeyframeTrack(`${name}.scale`, times, scl.get(name)));
  }

  for (const st of state.actions) {
    st.action.enabled = st.enabled;
    st.action.paused = st.paused;
    st.action.setEffectiveWeight(st.weight);
    st.action.loop = st.loop;
    st.action.clampWhenFinished = st.clamp;
    st.action.time = st.time;
  }
  mixer.timeScale = state.mixerScale;
  mixer.setTime(state.mixerTime);
  modelRoot.updateMatrixWorld(true);

  const clip = new THREE.AnimationClip('mixed_loop', duration, tracks);
  clip.userData = { target_fps: fps };
  return clip;
}

async function exportMixedLoopGlb(sampleFps = 60) {
  const clip = bakeMixedClip(sampleFps);
  const root = modelRoot;
  root.userData = { ...(root.userData || {}), target_fps: Number(sampleFps) || 60 };
  const exporter = new GLTFExporter();

  const arrayBuffer = await new Promise((resolve, reject) => {
    exporter.parse(
      root,
      (result) => {
        if (result instanceof ArrayBuffer) resolve(result);
        else reject(new Error('Expected binary GLB result'));
      },
      (err) => reject(err || new Error('GLTF export failed')),
      {
        binary: true,
        animations: [clip],
        onlyVisible: false,
        trs: false,
      }
    );
  });

  const dv = new DataView(arrayBuffer);
  if (dv.byteLength < 12) {
    throw new Error(`Exported buffer too small: ${dv.byteLength}`);
  }
  const magic = dv.getUint32(0, true);
  const version = dv.getUint32(4, true);
  const totalLength = dv.getUint32(8, true);
  if (magic !== 0x46546c67) {
    throw new Error('Invalid GLB magic');
  }
  if (version < 2) {
    throw new Error(`Unsupported GLB version: ${version}`);
  }
  if (totalLength !== dv.byteLength) {
    throw new Error(`GLB length mismatch: header=${totalLength}, actual=${dv.byteLength}`);
  }

  const blob = new Blob([arrayBuffer], { type: 'model/gltf-binary' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'mixed_loop.glb';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    try { document.body.removeChild(a); } catch (_e) {}
  }, 0);
  // Do not revoke immediately in Qt WebEngine, otherwise large files may be truncated.
  setTimeout(() => {
    try { URL.revokeObjectURL(url); } catch (_e) {}
  }, 120000);
  return true;
}

function rebuildSlotActions() {
  if (!mixer || !modelRoot) return;
  clearSlotActions();

  const ordered = Array.from(slots.values())
    .filter((s) => s.sourceClip && s.weight > 0.0001)
    .sort((a, b) => a.id - b.id);
  const meta = ordered.map((slot) => ({ slot, ...analyzeSlot(slot) }));

  const laterOverrideBones = new Array(meta.length).fill(null);
  const seenLaterOverride = new Set();
  for (let i = meta.length - 1; i >= 0; i -= 1) {
    laterOverrideBones[i] = new Set(seenLaterOverride);
    if ((meta[i].slot.mode || 'normal') === 'override') {
      for (const b of meta[i].movingBones) seenLaterOverride.add(b);
    }
  }

  const occupiedBones = new Set();
  for (let i = 0; i < meta.length; i += 1) {
    const m = meta[i];
    const slot = m.slot;
    const mode = slot.mode || 'normal';
    const blockedByLaterOverride = laterOverrideBones[i];

    const baseAllowed = new Set();
    const sourceBones = mode === 'normal' ? m.allBones : m.movingBones;
    for (const b of sourceBones) {
      if (blockedByLaterOverride.has(b)) continue;
      baseAllowed.add(b);
    }

    let allowed = baseAllowed;
    if (mode === 'additive') {
      allowed = new Set();
      for (const b of baseAllowed) {
        if (!occupiedBones.has(b)) allowed.add(b);
      }
    }
    if (!allowed.size) continue;

    const onlyMoving = mode === 'override' || mode === 'additive';
    const additive = mode === 'additive';
    const runtimeClip = buildRuntimeClip(slot, allowed, m.movingByTrack, onlyMoving, additive);
    if (!runtimeClip) continue;

    slot.filteredClip = runtimeClip;
    const action = mixer.clipAction(runtimeClip, modelRoot);
    slot.action = action;
    action.blendMode = additive ? THREE.AdditiveAnimationBlendMode : THREE.NormalAnimationBlendMode;
    action.enabled = true;
    action.loop = slot.loop ? THREE.LoopRepeat : THREE.LoopOnce;
    action.clampWhenFinished = !slot.loop;
    action.setEffectiveWeight(slot.weight);
    action.reset().play();

    for (const b of allowed) occupiedBones.add(b);
  }
}

function applyRenderLighting() {
  applyNoLightingMode(renderState.uniformLighting);
  if (renderState.uniformLighting) {
    sun.intensity = 0;
    hemi.intensity = 0;
    return;
  }
  sun.intensity = renderState.sunIntensity;
  hemi.intensity = renderState.ambientIntensity;
}

function toNoLightMaterial(src) {
  const m = new THREE.MeshBasicMaterial();
  m.name = src.name || '';
  m.map = src.map || null;
  m.alphaMap = src.alphaMap || null;
  m.transparent = !!src.transparent;
  m.opacity = Number.isFinite(src.opacity) ? src.opacity : 1.0;
  m.alphaTest = Number.isFinite(src.alphaTest) ? src.alphaTest : 0.0;
  m.side = src.side;
  m.depthTest = src.depthTest;
  m.depthWrite = src.depthWrite;
  m.visible = src.visible !== false;
  m.vertexColors = !!src.vertexColors;
  m.skinning = !!src.skinning;
  m.morphTargets = !!src.morphTargets;
  m.morphNormals = !!src.morphNormals;
  m.toneMapped = false;
  m.color.setScalar(Math.max(0.0, Number(renderState.uniformIntensity) || 1.0));
  return m;
}

function applyNoLightingMode(enabled) {
  if (!modelRoot) return;
  const want = !!enabled;
  if (want === noLightingApplied) {
    if (want) {
      const bright = Math.max(0.0, Number(renderState.uniformIntensity) || 1.0);
      for (const m of materials) {
        if (!m || !m.isMaterial) continue;
        if (m.color) {
          m.color.setScalar(bright);
          m.userData.__baseColor = m.color.clone();
        }
      }
    }
    return;
  }

  modelRoot.traverse((obj) => {
    if (!obj.isMesh || !obj.material) return;
    if (want) {
      if (!obj.userData.__litMaterial) {
        obj.userData.__litMaterial = obj.material;
      }
      if (Array.isArray(obj.material)) {
        obj.material = obj.material.map((src) => toNoLightMaterial(src));
      } else {
        obj.material = toNoLightMaterial(obj.material);
      }
    } else if (obj.userData.__litMaterial) {
      obj.material = obj.userData.__litMaterial;
      delete obj.userData.__litMaterial;
    }
  });

  noLightingApplied = want;
  collectMaterials(modelRoot);
  for (const kind of ['diff']) {
    applyTextureMap(kind, textureSlots[kind]);
  }
  applyTextureTuning();
}

function applySunDirection() {
  const az = (renderState.sunAzimuthDeg * Math.PI) / 180.0;
  const el = (renderState.sunElevationDeg * Math.PI) / 180.0;
  const r = 12;
  const x = Math.cos(el) * Math.cos(az) * r;
  const y = Math.sin(el) * r;
  const z = Math.cos(el) * Math.sin(az) * r;
  sun.position.set(x, y, z);
}

function collectMaterials(root) {
  materials.length = 0;
  root.traverse((obj) => {
    if (!obj.isMesh || !obj.material) return;
    const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
    for (const m of mats) {
      if (!m || materials.includes(m)) continue;
      if (!m.userData.__baseColor && m.color) {
        m.userData.__baseColor = m.color.clone();
      }
      materials.push(m);
    }
  });
}

function applyTextureTuning() {
  for (const m of materials) {
    if (m.color && m.userData.__baseColor) {
      m.color.copy(m.userData.__baseColor).multiplyScalar(textureState.diffBrightness);
    }
    m.needsUpdate = true;
  }
}

function applyTextureMap(kind, tex) {
  if (kind !== 'diff') return;
  textureSlots[kind] = tex || null;
  for (const m of materials) {
    if (kind === 'diff') {
      m.map = tex;
    }
    m.needsUpdate = true;
  }
  applyTextureTuning();
}

function textureDimensions(tex) {
  if (!tex) return { w: 0, h: 0 };
  const iw = tex.image && Number(tex.image.width);
  const ih = tex.image && Number(tex.image.height);
  if (Number.isFinite(iw) && Number.isFinite(ih) && iw > 0 && ih > 0) {
    return { w: iw, h: ih };
  }
  if (Array.isArray(tex.mipmaps) && tex.mipmaps.length > 0) {
    const m0 = tex.mipmaps[0];
    const mw = m0 && Number(m0.width);
    const mh = m0 && Number(m0.height);
    if (Number.isFinite(mw) && Number.isFinite(mh) && mw > 0 && mh > 0) {
      return { w: mw, h: mh };
    }
  }
  return { w: 0, h: 0 };
}

function configureTexture(kind, tex) {
  if (!tex) return false;
  const dims = textureDimensions(tex);
  if (dims.w <= 0 || dims.h <= 0) {
    return false;
  }
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.generateMipmaps = false;
  if (tex.repeat && typeof tex.repeat.set === 'function') {
    tex.repeat.set(textureState.tileU, textureState.tileV);
  }
  tex.colorSpace = kind === 'diff' ? THREE.SRGBColorSpace : THREE.NoColorSpace;
  tex.needsUpdate = true;
  return true;
}

function loadTextureByPath(relPath, kind, onLoad, onError) {
  const url = `./${relPath}`;
  const lower = String(relPath).toLowerCase();
  if (lower.endsWith('.dds')) {
    ddsLoader.load(
      url,
      (tex) => {
        if (!configureTexture(kind, tex)) {
          const d = textureDimensions(tex);
          onError(new Error(`Invalid DDS texture for ${kind} (w=${d.w}, h=${d.h})`));
          return;
        }
        onLoad(tex);
      },
      undefined,
      onError
    );
    return;
  }
  texLoader.load(
    url,
    (tex) => {
      if (!configureTexture(kind, tex)) {
        const d = textureDimensions(tex);
        onError(new Error(`Invalid texture for ${kind} (w=${d.w}, h=${d.h})`));
        return;
      }
      onLoad(tex);
    },
    undefined,
    onError
  );
}

async function ensureSlotLoaded(slotId, url, label) {
  const id = Number(slotId);
  const slot = getOrCreateSlot(id);
  if (slot.sourceClip && slot.url === url) {
    return true;
  }
  const requestRevision = bumpSlotRevision(id);

  try {
    setStatus(`Loading slot ${slot.id}: ${label}`);
    const gltf = await loadGltf(`./${url}`);
    if (requestRevision !== getSlotRevision(id)) {
      return false;
    }
    const liveSlot = getSlot(id);
    if (!liveSlot) {
      return false;
    }
    const clip = (gltf.animations || [])[0];
    if (!clip) {
      setStatus(`No animations in ${url}`);
      return false;
    }

    liveSlot.sourceClip = clip;
    liveSlot.url = url;
    liveSlot.label = label || clip.name || `slot_${slot.id}`;
    rebuildSlotActions();
    setStatus(`Slot ${slot.id} active: ${liveSlot.label} (${liveSlot.mode})`);
    return true;
  } catch (e) {
    console.error(e);
    setStatus(`Slot ${slot.id} load failed`);
    return false;
  }
}

window.viewerApi = {
  async setSlot(slotId, url, label) {
    return ensureSlotLoaded(slotId, url, label);
  },
  removeSlot(slotId) {
    const id = Number(slotId);
    bumpSlotRevision(id);
    const slot = getSlot(id);
    if (!slot) return false;
    if (mixer && modelRoot && slot.action) {
      slot.action.stop();
      mixer.uncacheAction(slot.action.getClip(), modelRoot);
      slot.action = null;
    }
    if (mixer && slot.filteredClip) {
      mixer.uncacheClip(slot.filteredClip);
      slot.filteredClip = null;
    }
    slots.delete(id);
    rebuildSlotActions();
    return true;
  },
  setSlotWeight(slotId, weight) {
    const slot = getOrCreateSlot(slotId);
    slot.weight = Math.max(0, Number(weight) || 0);
    rebuildSlotActions();
    return true;
  },
  setSlotMode(slotId, mode) {
    const slot = getOrCreateSlot(slotId);
    const m = String(mode || '').toLowerCase();
    slot.mode = (m === 'override' || m === 'additive') ? m : 'normal';
    rebuildSlotActions();
    return true;
  },
  setSlotLoop(slotId, loop) {
    const slot = getOrCreateSlot(slotId);
    slot.loop = !!loop;
    if (slot.action) {
      slot.action.loop = slot.loop ? THREE.LoopRepeat : THREE.LoopOnce;
      slot.action.clampWhenFinished = !slot.loop;
    }
    return true;
  },
  setGlobalSpeed(speed) {
    if (mixer) mixer.timeScale = Number(speed);
    return true;
  },
  resetCamera() {
    if (modelRoot) frameObject(modelRoot);
    return true;
  },
  setGridVisible(v) {
    grid.visible = !!v;
    return true;
  },
  setBackgroundGray(level) {
    const x = Math.max(0, Math.min(255, Number(level) || 63));
    scene.background = new THREE.Color(x / 255, x / 255, x / 255);
    return true;
  },
  setSunAngles(azimuthDeg, elevationDeg) {
    renderState.sunAzimuthDeg = Number(azimuthDeg) || 0;
    renderState.sunElevationDeg = Number(elevationDeg) || 0;
    applySunDirection();
    return true;
  },
  setSunIntensity(v) {
    renderState.sunIntensity = Number(v) || 0;
    applyRenderLighting();
    return true;
  },
  setAmbientIntensity(v) {
    renderState.ambientIntensity = Number(v) || 0;
    applyRenderLighting();
    return true;
  },
  setUniformLighting(enabled) {
    renderState.uniformLighting = !!enabled;
    applyRenderLighting();
    return true;
  },
  setUniformIntensity(v) {
    renderState.uniformIntensity = Number(v) || 0;
    applyRenderLighting();
    return true;
  },
  setTextureMap(kind, url) {
    if (kind !== 'diff') return false;
    loadTextureByPath(
      url,
      kind,
      (tex) => {
        applyTextureMap(kind, tex);
        setStatus(`Texture applied: ${kind}`);
      },
      (err) => {
        console.error(err);
        setStatus(`Texture load failed: ${kind}`);
      }
    );
    return true;
  },
  clearTextureMap(kind) {
    if (kind !== 'diff') return false;
    applyTextureMap(kind, null);
    return true;
  },
  setTextureTiling(u, v) {
    textureState.tileU = Math.max(0.01, Number(u) || 1.0);
    textureState.tileV = Math.max(0.01, Number(v) || 1.0);
    for (const tex of Object.values(textureSlots)) {
      if (!tex) continue;
      tex.wrapS = THREE.RepeatWrapping;
      tex.wrapT = THREE.RepeatWrapping;
      if (tex.repeat && typeof tex.repeat.set === 'function') {
        tex.repeat.set(textureState.tileU, textureState.tileV);
      }
      tex.needsUpdate = true;
    }
    return true;
  },
  setDiffBrightness(v) {
    textureState.diffBrightness = Number(v) || 1;
    applyTextureTuning();
    return true;
  },
  async exportMixedLoopGlb(sampleFps) {
    try {
      setStatus('Exporting mixed GLB...');
      await exportMixedLoopGlb(Number(sampleFps) || 60);
      setStatus(`Export finished (baked at ${Number(sampleFps) || 60} Hz)`);
      return true;
    } catch (e) {
      console.error(e);
      setStatus(`Export failed: ${e?.message || e}`);
      return false;
    }
  },
  setLiveLinkEnabled(enabled) {
    liveLinkEnabled = !!enabled;
    if (liveLinkEnabled) {
      pollLiveLink();
      startLivePolling();
    } else {
      stopLivePolling();
    }
    return true;
  },
};

async function main() {
  if (!manifest) throw new Error('Manifest missing');
  await replaceModel(manifest.model, true);

  setStatus('Ready (slot mode)');
}

(function animate() {
  requestAnimationFrame(animate);
  resize();
  const dt = clock.getDelta();
  if (mixer) mixer.update(dt);
  controls.update();
  renderer.render(scene, camera);
})();

main().catch((e) => {
  console.error(e);
  setStatus(`ERROR: ${e?.message || e}`);
});

window.addEventListener('error', (ev) => {
  const msg = ev?.message || 'Unknown JS error';
  console.error(ev?.error || msg);
  setStatus(`JS ERROR: ${msg}`);
});
