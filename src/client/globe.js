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
  fetch('/land-110m.geojson').then(response => { if (!response.ok) throw new Error('Map unavailable'); return response.json(); }).then(drawLand).catch(error => console.warn('海岸線を読み込めませんでした', error));
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
  earth.rotation.y = 0;
  // 範囲（国）の目印と選択。app.js が window.MISE_SCOPE = {countries:[{code,lat,lon}], current} を置き、変わるたびに 'mise-scope-changed' を発火する。
  // 選べる国（サイトが 1 件以上ある国）にだけ小さな点を置き、現在の範囲の国は大きく暖色で示す。背景（地球）をクリック/タップすると近い国を範囲に選ぶ。
  const markers = new Map();
  const markerMaterial = new THREE.MeshBasicMaterial({color: 0x007e90}), markerActiveMaterial = new THREE.MeshBasicMaterial({color: 0xdf7f3e});
  const markerGeometry = new THREE.SphereGeometry(1, 12, 8);
  function syncMarkers() {
    const scope = window.MISE_SCOPE || {countries: [], current: 'all'};
    for (const marker of markers.values()) earth.remove(marker);
    markers.clear();
    for (const c of scope.countries) {
      if (!Number.isFinite(c.lat) || !Number.isFinite(c.lon)) continue;
      const active = c.code.toLowerCase() === scope.current;
      const marker = new THREE.Mesh(markerGeometry, active ? markerActiveMaterial : markerMaterial);
      marker.scale.setScalar(active ? .045 : .03);
      marker.position.set(...globePoint(c.lon, c.lat, 1.63));
      earth.add(marker); markers.set(c.code, marker);
    }
  }
  window.addEventListener('mise-scope-changed', syncMarkers); syncMarkers();
  const raycaster = new THREE.Raycaster(), pointer = new THREE.Vector2();
  // 画面上の点が当たる緯度経度（地球に当たらなければ null）。globePoint の逆変換。
  function pickLonLat(clientX, clientY) {
    pointer.set(clientX / innerWidth * 2 - 1, -(clientY / innerHeight) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObject(earth, false)[0];
    if (!hit) return null;
    const p = earth.worldToLocal(hit.point.clone()).normalize();
    const lat = THREE.MathUtils.radToDeg(Math.asin(p.y));
    const phi = (Math.atan2(p.z, -p.x) + Math.PI * 2) % (Math.PI * 2);
    return { lat, lon: phi / (Math.PI * 2) * 360 - 180 };
  }
  // 当たった点に最も近い選べる国（角距離 12° 以内）。国境の多角形は持たないので代表点からの距離で選ぶ。
  const PICK_RADIUS_DEG = 12;
  function countryAt(clientX, clientY) {
    const at = pickLonLat(clientX, clientY); if (!at) return null;
    const rad = THREE.MathUtils.degToRad, a = rad(at.lat);
    let best = null, bestDistance = rad(PICK_RADIUS_DEG);
    for (const c of (window.MISE_SCOPE?.countries || [])) {
      if (!Number.isFinite(c.lat) || !Number.isFinite(c.lon)) continue;
      const b = rad(c.lat), d = Math.acos(Math.min(1, Math.max(-1, Math.sin(a) * Math.sin(b) + Math.cos(a) * Math.cos(b) * Math.cos(rad(at.lon - c.lon)))));
      if (d < bestDistance) { best = c.code; bestDistance = d; }
    }
    return best;
  }
  // 何も載っていない背景（html/body と、本文の入れ物の余白）だけが地球の入力になる。リンク・ボタン・文章の上では選ばない。
  const BACKGROUND = 'main, header, .logo-row, .search-row, #directory, #categories, #results, footer, hr';
  const isBackground = el => el === document.documentElement || el === document.body || (el instanceof Element && el.matches(BACKGROUND));
  let targetRotation = 0, targetTilt = 0, targetDistance = 6.6;
  let flying = false;
  // 国への向き移動（旗のホバー/タップ）。flying は立てない: 無操作 IDLE_RESUME_MS 後に既定の向き・距離へ戻り自動回転が再開する。
  // peekHeld=true の間（ホバー/フォーカス中）は戻らない。mise-fly-release か、ホールドなしのイベントで解除。
  // 区画（県・州）は国より近い。定数は docs/design/architecture.md と対応。
  const COUNTRY_DISTANCE = 4.2, REGION_DISTANCE = 3.2;
  let peek = false, peekHeld = false;
  // 緯度経度の点が画面正面(+z)へ最も向く自転角 y と傾き x を探す（粗い格子探索のあと細かく詰める）。
  function aimAt(lon, lat) {
    const [x, y, z] = globePoint(lon, lat, 1);
    const score = (ry, rx) => {
      const z1 = -x * Math.sin(ry) + z * Math.cos(ry);
      return y * Math.sin(rx) + z1 * Math.cos(rx); // 回転後の z 成分（1 なら正面）。軸傾斜は視線まわりの回転なので向きに影響しない
    };
    let best = [0, 0, -2];
    const search = (ry0, rx0, span, step) => {
      for (let ry = ry0 - span; ry <= ry0 + span; ry += step) for (let rx = Math.max(-1.2, rx0 - span); rx <= Math.min(1.2, rx0 + span); rx += step) {
        const v = score(ry, rx); if (v > best[2]) best = [ry, rx, v];
      }
    };
    search(Math.PI, 0, Math.PI, .04); search(best[0], best[1], .05, .004);
    return { rotation: best[0], tilt: best[1] };
  }
  function flyToCountry(event) {
    const { lat, lon, hold, level } = event.detail || {};
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    const aim = aimAt(lon, lat), TAU = Math.PI * 2;
    // 現在の向きから最短で回る（無駄に何周もしない）
    const rotation = aim.rotation + TAU * Math.round((earth.rotation.y - aim.rotation) / TAU);
    flying = false; peek = true; peekHeld = !!hold; lastActiveAt = performance.now();
    targetRotation = rotation; targetTilt = aim.tilt; targetDistance = level === 'region' ? REGION_DISTANCE : COUNTRY_DISTANCE;
    if (reducedMotion.matches) { earth.rotation.y = targetRotation; earth.rotation.x = targetTilt; camera.position.z = targetDistance; }
  }
  // 既定の見え方（世界全体）へ即座に戻す指示。ロゴ・トップ・範囲 all への移動で app.js が発火する。向きはそのまま、傾きと距離を戻し、自動回転は最後の操作から IDLE_RESUME_MS 後に再開する。
  function resetView() {
    flying = false; peek = false; peekHeld = false; lastActiveAt = performance.now();
    targetTilt = 0; targetDistance = 6.6;
    if (reducedMotion.matches) { earth.rotation.x = 0; camera.position.z = 6.6; }
  }
  function releasePeek() { if (!peekHeld) return; peekHeld = false; lastActiveAt = performance.now(); }
  let drag = null;
  // 自動回転の再開: 最後の操作（実際のドラッグ移動・国への移動・ホバー解除）から IDLE_RESUME_MS 無操作で再開。
  // 「地球から日本へ」(flying) の間だけは再開しない（地球に戻るボタンで解除）。
  const IDLE_RESUME_MS = 4000;
  let lastActiveAt = -Infinity, spinGain = 0;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  window.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target.closest('a, button, input, textarea, select, label')) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
  });
  window.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!dx && !dy) return;
    targetRotation += dx * .008;
    targetTilt = Math.max(-1.2, Math.min(1.2, targetTilt + dy * .005));
    lastActiveAt = performance.now(); drag.moved = true; peek = false;
    drag.x = event.clientX; drag.y = event.clientY;
  });
  // 動かさずに離しただけ（背景クリック等）では時刻を更新しない。
  const endDrag = event => {
    if (drag && event.pointerId === drag.id) {
      if (drag.moved) lastActiveAt = performance.now();
      else if (event.type === 'pointerup' && isBackground(event.target)) { const code = countryAt(event.clientX, event.clientY); if (code) window.dispatchEvent(new CustomEvent('mise-globe-pick', {detail: {code}})); }
      drag = null;
    }
  };
  // 選べる国の上にポインターがあるときはカーソルを手の形にする（CSS: html.globe-hot）。
  let hotAt = 0, hot = false;
  window.addEventListener('pointermove', event => {
    if (drag || event.pointerType === 'touch' || performance.now() - hotAt < 60) return;
    hotAt = performance.now();
    const next = isBackground(event.target) && !!countryAt(event.clientX, event.clientY);
    if (next !== hot) { hot = next; document.documentElement.classList.toggle('globe-hot', hot); }
  });
  window.addEventListener('pointerup', endDrag);
  window.addEventListener('pointercancel', endDrag);
  window.addEventListener('mise-fly-country', flyToCountry);
  window.addEventListener('mise-fly-release', releasePeek);
  window.addEventListener('mise-fly-reset', resetView);
  // 計測・診断用の読み取り表示（data-distance/target/peek）。値が変わったときだけ書く。
  const shown = {};
  function publishState() {
    const next = { distance: camera.position.z.toFixed(2), target: targetDistance.toFixed(2), peek: peek ? '1' : '0' };
    for (const k in next) if (shown[k] !== next[k]) { shown[k] = next[k]; host.dataset[k] = next[k]; }
  }
  function resize() { renderer.setSize(innerWidth, innerHeight); camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); }
  window.addEventListener('resize', resize); resize();
  // タブが非表示の間はレンダを止める（復帰時に再開）。
  let rafId = 0;
  function animate() {
    rafId = requestAnimationFrame(animate);
    const idle = !drag && !peekHeld && performance.now() - lastActiveAt >= IDLE_RESUME_MS;
    if (peek && idle) {
      peek = false; targetTilt = 0; targetDistance = 6.6;
      if (reducedMotion.matches) { earth.rotation.x = 0; camera.position.z = 6.6; }
    }
    const canSpin = !flying && !peek && !reducedMotion.matches && idle
      && Math.abs(camera.position.z - targetDistance) < 0.02;
    // 再開時は速度を 0 から滑らかに立ち上げる（急な速度変化を避ける）。
    spinGain += ((canSpin ? 1 : 0) - spinGain) * .03;
    if (canSpin) targetRotation += 0.0008 * spinGain;
    earth.rotation.y += (targetRotation - earth.rotation.y) * .04;
    earth.rotation.x += (targetTilt - earth.rotation.x) * .04;
    camera.position.z += (targetDistance - camera.position.z) * .04;
    publishState();
    renderer.render(scene, camera);
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { cancelAnimationFrame(rafId); rafId = 0; } else if (!rafId) { spinGain = 0; animate(); }
  });
  if (!document.hidden) animate();
} catch (error) {
  host.hidden = true;
  console.warn('3D地球を表示できませんでした', error);
}
