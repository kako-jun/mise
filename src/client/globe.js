import * as THREE from '../../vendor/three.module.js';

const host = document.querySelector('#globe-background');
try {
  const renderer = new THREE.WebGLRenderer({alpha: true, antialias: true});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  host.append(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
  camera.position.z = 6.6;
  const canvas = document.createElement('canvas'); canvas.width = 1024; canvas.height = 512;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#c9eff1'; ctx.fillRect(0, 0, 1024, 512);
  ctx.fillStyle = '#a0dbe3'; ctx.strokeStyle = '#008a9d'; ctx.lineWidth = 0.6;
  // Natural Earth land polygons are drawn in equirectangular coordinates.
  // Source: https://www.naturalearthdata.com/ (public domain), 1:110m land.
  function drawLand(data) {
    for (const feature of data.features) {
      const geometry = feature.geometry;
      const polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
      for (const polygon of polygons) {
        ctx.beginPath();
        for (const ring of polygon) {
          ring.forEach(([lon, lat], i) => {
            const x = (lon + 180) / 360 * canvas.width;
            const y = (90 - lat) / 180 * canvas.height;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          });
          ctx.closePath();
        }
        ctx.fill('evenodd'); ctx.stroke();
      }
    }
    texture.needsUpdate = true;
  }
  const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace;
  fetch('./land-110m.geojson').then(response => { if (!response.ok) throw new Error('Map unavailable'); return response.json(); }).then(drawLand).catch(error => console.warn('海岸線を読み込めませんでした', error));
  const earth = new THREE.Mesh(new THREE.SphereGeometry(1.6, 64, 32), new THREE.MeshPhongMaterial({map: texture, shininess: 10}));
  const axialTilt = new THREE.Group();
  axialTilt.rotation.z = THREE.MathUtils.degToRad(23.44);
  axialTilt.add(earth); scene.add(axialTilt);
  // Approximate ocean anchors: Atlantic (52°N, 30°W) and Pacific (52°N, 155°W).
  // Base intervals were sampled against Natural Earth 1:110m and lie over ocean.
  const earMaterial = new THREE.MeshBasicMaterial({color: 0x59dce4, side: THREE.DoubleSide});
  const earOutlineMaterial = new THREE.LineBasicMaterial({color: 0x007e90});
  function globePoint(lon, lat, radius) {
    const u = (lon + 180) / 360, phi = u * Math.PI * 2, a = THREE.MathUtils.degToRad(lat);
    return [-radius * Math.cos(a) * Math.cos(phi), radius * Math.sin(a), radius * Math.cos(a) * Math.sin(phi)];
  }
  for (const lon of [-30, -155]) {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute([
      ...globePoint(lon - 16, 52, 1.61), ...globePoint(lon + 16, 52, 1.61),
      ...globePoint(lon + 1.8, 64, 1.93), ...globePoint(lon - 1.8, 64, 1.93)
    ], 3));
    geometry.setIndex([0, 1, 2, 0, 2, 3]);
    geometry.computeVertexNormals();
    earth.add(new THREE.Mesh(geometry, earMaterial), new THREE.LineSegments(new THREE.EdgesGeometry(geometry), earOutlineMaterial));
  }
  // Latitude and longitude curves create a regular electronic grid.
  const gridMaterial = new THREE.LineBasicMaterial({color: 0x008b9e, transparent: true, opacity: .65});
  const radius = 1.609;
  // 128 本の折れ線を 1 本の LineSegments にまとめ、draw call を減らす（見た目は同一）。
  const gridPositions = [];
  function addGrid(points) {
    for (let i = 1; i < points.length; i++) {
      gridPositions.push(points[i - 1].x, points[i - 1].y, points[i - 1].z, points[i].x, points[i].y, points[i].z);
    }
  }
  for (let lat = -75; lat <= 75; lat += 4) {
    const points = []; const a = THREE.MathUtils.degToRad(lat);
    for (let lon = 0; lon <= 360; lon += 3) {
      const b = THREE.MathUtils.degToRad(lon);
      points.push(new THREE.Vector3(radius * Math.cos(a) * Math.cos(b), radius * Math.sin(a), radius * Math.cos(a) * Math.sin(b)));
    }
    addGrid(points);
  }
  for (let lon = 0; lon < 360; lon += 4) {
    const points = []; const b = THREE.MathUtils.degToRad(lon);
    for (let lat = -90; lat <= 90; lat += 3) {
      const a = THREE.MathUtils.degToRad(lat);
      points.push(new THREE.Vector3(radius * Math.cos(a) * Math.cos(b), radius * Math.sin(a), radius * Math.cos(a) * Math.sin(b)));
    }
    addGrid(points);
  }
  const gridGeometry = new THREE.BufferGeometry();
  gridGeometry.setAttribute('position', new THREE.Float32BufferAttribute(gridPositions, 3));
  earth.add(new THREE.LineSegments(gridGeometry, gridMaterial));
  scene.add(new THREE.AmbientLight(0xffffff, 2));
  const light = new THREE.DirectionalLight(0xffffff, 1.2); light.position.set(-3, 3, 5); scene.add(light);
  // The texture's longitude around Japan is 138° east.
  const japanRotation = 2.22;
  earth.rotation.y = 0;
  let targetRotation = 0, targetTilt = 0, targetDistance = 6.6;
  let flying = false;
  function fly() { targetRotation = japanRotation; targetTilt = 0.62; targetDistance = 1.94; flying = true; }
  function reset() { targetRotation = 0; targetTilt = 0; targetDistance = 6.6; flying = false; }
  let drag = null;
  // 手動回転後の自動回転再開: ドラッグ終了から IDLE_RESUME_MS 無操作で再開（flying=飛行中は再開しない）。
  const IDLE_RESUME_MS = 4000;
  let lastDragAt = -Infinity, spinGain = 0;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  window.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target.closest('a, button, input, textarea, select, label')) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY };
  });
  window.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!dx && !dy) return;
    targetRotation += dx * .008;
    targetTilt = Math.max(-1.2, Math.min(1.2, targetTilt + dy * .005));
    lastDragAt = performance.now();
    drag.x = event.clientX; drag.y = event.clientY;
  });
  const endDrag = event => { if (drag && event.pointerId === drag.id) { drag = null; lastDragAt = performance.now(); } };
  window.addEventListener('pointerup', endDrag);
  window.addEventListener('pointercancel', endDrag);
  window.addEventListener('mise-fly-japan', fly);
  document.querySelector('#fly-japan')?.addEventListener('click', fly);
  document.querySelector('#reset-globe')?.addEventListener('click', reset);
  // globe.js の読込前に押されたボタンを再生する。
  if (window.__misePendingGlobe === 'fly-japan') fly(); else if (window.__misePendingGlobe === 'reset-globe') reset();
  function resize() { renderer.setSize(innerWidth, innerHeight); camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); }
  window.addEventListener('resize', resize); resize();
  // タブが非表示の間はレンダを止める（復帰時に再開）。
  let rafId = 0;
  function animate() {
    rafId = requestAnimationFrame(animate);
    const canSpin = !flying && !drag && !reducedMotion.matches
      && performance.now() - lastDragAt >= IDLE_RESUME_MS
      && Math.abs(camera.position.z - targetDistance) < 0.02;
    // 再開時は速度を 0 から滑らかに立ち上げる（急な速度変化を避ける）。
    spinGain += ((canSpin ? 1 : 0) - spinGain) * .03;
    if (canSpin) targetRotation += 0.0008 * spinGain;
    earth.rotation.y += (targetRotation - earth.rotation.y) * .04;
    earth.rotation.x += (targetTilt - earth.rotation.x) * .04;
    camera.position.z += (targetDistance - camera.position.z) * .04;
    renderer.render(scene, camera);
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { cancelAnimationFrame(rafId); rafId = 0; } else if (!rafId) { spinGain = 0; animate(); }
  });
  if (!document.hidden) animate();
} catch (error) {
  host.hidden = true;
  document.querySelector('.globe-control')?.setAttribute('hidden', '');
  console.warn('3D地球を表示できませんでした', error);
}
