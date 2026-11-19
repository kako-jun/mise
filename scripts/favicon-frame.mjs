// check-pwa.mjs と tests/favicon-frame.test.mjs が使う、PNG の復号と 16px ファビコンの検査（#151）。
import zlib from 'node:zlib';

// PNG を自前で復号する（8bit RGB/RGBA・非インターレースのみ）
export function decodePng(buf) {
  const w = buf.readUInt32BE(16), h = buf.readUInt32BE(20), type = buf[25], bpp = type === 6 ? 4 : 3;
  if (buf[24] !== 8 || (type !== 6 && type !== 2) || buf[28] !== 0) throw new Error('未対応の PNG 形式');
  const idat = []; for (let o = 8; o < buf.length;) { const n = buf.readUInt32BE(o); if (buf.toString('latin1', o + 4, o + 8) === 'IDAT') idat.push(buf.subarray(o + 8, o + 8 + n)); o += 12 + n; }
  const raw = zlib.inflateSync(Buffer.concat(idat)), stride = w * bpp, px = Buffer.alloc(h * stride);
  for (let y = 0; y < h; y++) {
    const f = raw[y * (stride + 1)], src = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? px[y * stride + x - bpp] : 0, b = y ? px[(y - 1) * stride + x] : 0, c = x >= bpp && y ? px[(y - 1) * stride + x - bpp] : 0;
      const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
      px[y * stride + x] = (src[x] + (f === 0 ? 0 : f === 1 ? a : f === 2 ? b : f === 3 ? (a + b) >> 1 : (pa <= pb && pa <= pc ? a : pb <= pc ? b : c))) & 255;
    }
  }
  return { w, h, bpp, px };
}

// ICO（PNG 埋め込み）から指定サイズのフレームを取り出して復号する
export function icoFrame(ico, size) {
  for (let i = 0; i < ico.readUInt16LE(4); i++) {
    if (ico[6 + i * 16] !== size) continue;
    const off = ico.readUInt32LE(6 + i * 16 + 12), len = ico.readUInt32LE(6 + i * 16 + 8);
    return decodePng(ico.subarray(off, off + len));
  }
  return null;
}

// 16px フレームが「M」として成立しているかを調べ、問題の一覧を返す（空なら OK）。RGBA 必須。
// 座標は 16px の実フレームに基づく: 脚は列 3〜5 と 10〜12、中央の切れ込みは列 6〜9、下の脚の隙間は列 4〜6 と 9〜11。
export function faviconProblems({ w, h, bpp, px }) {
  if (w !== 16 || h !== 16 || bpp !== 4) return ['16x16 の RGBA ではない'];
  const at = (x, y) => { const o = (y * w + x) * 4; return { r: px[o], g: px[o + 1], b: px[o + 2], a: px[o + 3] }; };
  const dark = c => c.r + c.g + c.b < 250;
  const gapLike = c => c.a < 200 || dark(c); // 透明、または濃色の輪郭
  const problems = [];
  const colors = new Set(); let visible = 0;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) { const c = at(x, y); if (c.a > 128) { visible++; colors.add(`${c.r >> 4},${c.g >> 4},${c.b >> 4}`); } }
  if (visible < 100) problems.push(`見える画素が少なすぎる（${visible}）`);
  if (colors.size < 4) problems.push(`色数が少なすぎる（${colors.size}）`);
  // 脚: 切れ込みの左右の脚は不透明で暗くない（クリーム・橙・茶の面）
  for (const [name, xs] of [['左脚', [3, 4, 5]], ['右脚', [10, 11, 12]]]) {
    const bad = [];
    for (let y = 1; y <= 5; y++) for (const x of xs) { const c = at(x, y); if (c.a < 250 || dark(c)) bad.push(`(${x},${y})`); }
    if (bad.length) problems.push(`${name}の面が不透明・明色でない: ${bad.slice(0, 3).join('')}`);
  }
  // 上端の切れ込みが上から 6 行とも中央に見える
  for (let y = 0; y <= 5; y++) if (![6, 7, 8, 9].some(x => gapLike(at(x, y)))) problems.push(`行 ${y} に切れ込みが無い`);
  // 下の脚の隙間（2 本の脚の間）が左右ともどこかの行に見える
  for (const [name, xs] of [['左', [4, 5, 6]], ['右', [9, 10, 11]]]) {
    if (![10, 11, 12, 13, 14, 15].some(y => xs.some(x => gapLike(at(x, y))))) problems.push(`下の脚の${name}側の隙間が無い`);
  }
  return problems;
}
