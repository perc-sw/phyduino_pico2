// Switches the board panel between the rendered image and an interactive
// 3D model. The model viewer and the GLB file are only downloaded the first
// time the 3D view is selected.

// Paths are relative to this script, not the page.
const VIEWER_SCRIPT = new URL('../vendor/model-viewer-4.3.1/model-viewer.min.js', import.meta.url).href;
const MODEL_SRC = new URL('../models/phyduino_pico2.glb', import.meta.url).href;

const panel = document.querySelector('.board-img-panel');
const image = panel.querySelector('img');
const buttons = panel.querySelectorAll('.view-toggle button');
const status = panel.querySelector('.view-status');

let viewer = null;
let loading = null;
let current = 'image';

function setStatus(text) {
  status.textContent = text;
  status.hidden = !text;
}

async function createViewer() {
  await import(VIEWER_SCRIPT);

  const el = document.createElement('model-viewer');
  el.setAttribute('src', MODEL_SRC);
  el.setAttribute('alt', 'Interactive 3D model of the Phyduino Pico2 board');
  el.setAttribute('camera-controls', '');
  el.setAttribute('touch-action', 'pan-y');
  el.setAttribute('camera-orbit', '0deg 55deg auto');
  el.setAttribute('shadow-intensity', '0.6');
  el.setAttribute('exposure', '0.9');
  el.setAttribute('interaction-prompt', 'none');

  el.addEventListener('progress', (e) => {
    const pct = Math.round(e.detail.totalProgress * 100);
    if (current === '3d') setStatus(pct < 100 ? `Loading 3D model… ${pct}%` : '');
  });
  el.addEventListener('error', () => {
    if (current === '3d') setStatus('The 3D model could not be loaded.');
  });

  panel.insertBefore(el, status);
  return el;
}

async function show(view) {
  current = view;
  buttons.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === view)));

  if (view === '3d') {
    image.hidden = true;
    if (!viewer) {
      setStatus('Loading 3D model…');
      loading ??= createViewer();
      try {
        viewer = await loading;
      } catch {
        loading = null;
        if (current === '3d') setStatus('The 3D viewer could not be loaded.');
        return;
      }
    }
    // The selection may have changed while the viewer was loading.
    viewer.hidden = current !== '3d';
  } else {
    if (viewer) viewer.hidden = true;
    setStatus('');
    image.hidden = false;
  }
}

buttons.forEach((b) => b.addEventListener('click', () => show(b.dataset.view)));
panel.querySelector('.view-toggle').hidden = false;
