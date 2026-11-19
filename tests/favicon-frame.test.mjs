// 16px ファビコンの検査（scripts/favicon-frame.mjs）が壊れた画像を通さないことの変異テスト（#151）。
// 実際の public/favicon.ico の 16px フレームを土台に、壊した画像を作って faviconProblems が必ず問題を返すことを確かめる。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { icoFrame, faviconProblems } from '../scripts/favicon-frame.mjs';

const ico = fs.readFileSync(fileURLToPath(new URL('../public/favicon.ico', import.meta.url)));
const base = icoFrame(ico, 16);
const CREAM = [0xff, 0xf1, 0xd6, 255], ORANGE = [0xee, 0x8a, 0x3c, 255];
const mutate = fn => { const f = { ...base, px: Buffer.from(base.px) }; fn(f.px, (x, y, c) => { if (x >= 0 && x < 16 && y >= 0 && y < 16) f.px.set(c, (y * 16 + x) * 4); }); return f; };
const get = (px, x, y) => Array.from(px.subarray((y * 16 + x) * 4, (y * 16 + x) * 4 + 4));

test('実物の 16px フレームは問題なし', () => assert.deepEqual(faviconProblems(base), []));

const mutations = {
  '橙の単色': mutate((px, set) => { for (let y = 0; y < 16; y++) for (let x = 0; x < 16; x++) set(x, y, ORANGE); }),
  '完全に透明': mutate((px, set) => { for (let y = 0; y < 16; y++) for (let x = 0; x < 16; x++) set(x, y, [0, 0, 0, 0]); }),
  '黒の単色': mutate((px, set) => { for (let y = 0; y < 16; y++) for (let x = 0; x < 16; x++) set(x, y, [0, 0, 0, 255]); }),
  '切れ込みをクリームで埋める': mutate((px, set) => { for (let y = 0; y <= 5; y++) for (let x = 6; x <= 9; x++) set(x, y, CREAM); }),
  '下半分をクリームで埋める': mutate((px, set) => { for (let y = 8; y < 16; y++) for (let x = 2; x <= 13; x++) set(x, y, CREAM); }),
  '3px 右ずらし': mutate((px, set) => { const o = Buffer.from(px); for (let y = 0; y < 16; y++) for (let x = 0; x < 16; x++) set(x, y, x >= 3 ? Array.from(o.subarray((y * 16 + x - 3) * 4, (y * 16 + x - 3) * 4 + 4)) : [0, 0, 0, 0]); }),
  '輪郭（濃色）をクリームにする': mutate((px, set) => { for (let y = 0; y < 16; y++) for (let x = 0; x < 16; x++) { const c = get(px, x, y); if (c[3] > 0 && c[0] + c[1] + c[2] < 250) set(x, y, CREAM); } }),
};
for (const [name, frame] of Object.entries(mutations)) {
  test(`変異「${name}」は NG になる`, () => assert.notDeepEqual(faviconProblems(frame), []));
}
