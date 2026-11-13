// public/land-110m.geojson を軽量化する（座標を小数2桁に丸め、重複点・property・bbox・crs を除去）。
// 描画は 1024px 幅のテクスチャ（1px≒0.35°）なので、0.01°（約1km）の丸めは見た目に影響しない。
import { readFileSync, writeFileSync } from 'node:fs';
const path = 'public/land-110m.geojson';
const src = JSON.parse(readFileSync(path, 'utf8'));
const r = n => Math.round(n * 100) / 100;
const ring = pts => { const out = []; for (const [x, y] of pts) { const p = [r(x), r(y)]; const l = out[out.length - 1]; if (!l || l[0] !== p[0] || l[1] !== p[1]) out.push(p); } return out; };
const features = src.features.map(f => ({ type: 'Feature', geometry: { type: f.geometry.type, coordinates: f.geometry.type === 'Polygon' ? f.geometry.coordinates.map(ring) : f.geometry.coordinates.map(poly => poly.map(ring)) } }));
writeFileSync(path, JSON.stringify({ type: 'FeatureCollection', name: src.name, features }));
