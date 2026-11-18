// 計測用: ローカル D1（miniflare の sqlite ファイル）に合成サイトを足して総数を targetTotal にする（決定的乱数。本番には使えない: ローカルのファイルを直接開く）。#126
// usage: [PROD_SHAPE=1] node scripts/bench/gen-synthetic-sites.mjs <sqliteFile> <targetTotal>
//   PROD_SHAPE=1: 本番の形（台帳の一括投入）にする。全体の 86% が bulk_import=1 で rowid が連続した塊になり、その日時（registered_at）がすべて同値（'2026-11-13 00:00:00'）、source_updated_at は NULL。
//   散らばった一括投入・同値の少ない日時では、同じ日時が大量に続く塊での読み取りの増え方（キーセットの一時 B-tree）とピックアップの偏りが出ない（#126 のレビューで判明）。
//   sqliteFile は `.wrangler/state/v3/d1/miniflare-D1DatabaseObject/<hash>.sqlite`（全 migration 適用済みであること）。実行前に dev サーバーを止める。
//   分布: 日本語 200 語・英語 190 語を Zipf 分布で並べた紹介文（ja 必須、en は 70%）、タグは 2,000 語彙から 0〜5 個、カテゴリは葉から 1〜3 個、国は JP 27% ほか、15% は bulk_import=1、40% に source_updated_at。
//   合成サイトの id は syn- で始まる。消すには DELETE FROM sites WHERE id LIKE 'syn-%'（site_tags・site_categories も同様）。
import { DatabaseSync } from 'node:sqlite';
import { searchText } from '../../src/sites.js';
const [file, target] = [process.argv[2], Number(process.argv[3])];
const db = new DatabaseSync(file), PROD_SHAPE = process.env.PROD_SHAPE === '1';
let seed = 12345; const rnd = () => { seed = (Math.imul(seed, 1103515245) + 12345) >>> 0; return seed / 2 ** 32; };
const pick = a => a[Math.floor(rnd() * a.length)];
const zipf = (n, s = 1.05) => { const w = Array.from({ length: n }, (_, i) => 1 / (i + 1) ** s); const tot = w.reduce((a, b) => a + b); const cum = []; let c = 0; for (const x of w) { c += x / tot; cum.push(c); } return () => { const r = rnd(); let lo = 0, hi = n - 1; while (lo < hi) { const m = (lo + hi) >> 1; if (cum[m] < r) lo = m + 1; else hi = m; } return lo; }; };
const JA = `サイト 情報 紹介 公式 写真 日記 ブログ 料理 レシピ 旅行 地域 歴史 文化 音楽 映画 読書 本 漫画 アニメ ゲーム 手作り 工房 雑貨 カフェ 喫茶 パン 菓子 和菓子 酒 日本酒 ワイン 珈琲 紅茶 茶 猫 犬 鳥 魚 花 植物 園芸 庭 農業 野菜 果物 米 漁業 山 川 海 島 温泉 宿 旅館 ホテル 観光 祭り 神社 寺 城 博物館 美術館 図書館 学校 大学 研究 教育 子育て 家族 健康 医療 病院 介護 福祉 スポーツ 野球 サッカー 釣り 登山 自転車 鉄道 車 バイク 飛行機 船 天気 気象 防災 地震 環境 自然 宇宙 天文 科学 数学 物理 化学 生物 技術 プログラミング ソフトウェア 開発 デザイン 写真家 作家 画家 職人 伝統 工芸 陶芸 木工 染物 着物 服 洋服 古着 帽子 靴 鞄 時計 眼鏡 家具 建築 住宅 不動産 店 商店 市場 通販 販売 会社 企業 事務所 法律 税金 会計 経済 政治 行政 市役所 町 村 県 東京 大阪 京都 北海道 沖縄 福岡 名古屋 横浜 神戸 金沢 仙台 広島 個人 趣味 研究会 同好会 団体 協会 地図 散歩 食べ歩き ラーメン 蕎麦 うどん 寿司 焼肉 定食 弁当 居酒屋 劇場 演劇 落語 囲碁 将棋 書道 俳句 短歌 詩 小説 翻訳 言語 英語 辞書 図鑑 資料 記録 年表 データ`.split(' ');
const RARE = '鯖鰯鰻鮎蛍楓椿梟狐狸鴨雉蕨筍栗柿梨桃杏柚'.split('');
const EN = `site official blog guide shop studio local history culture music film books comics games handmade crafts cafe bakery sweets sake wine coffee tea cats dogs birds fish flowers garden farm vegetables fruit rice fishing mountain river sea island hot spring inn hotel travel festival shrine temple castle museum gallery library school university research education family health medical hospital care sports baseball soccer cycling railway cars weather disaster nature space astronomy science math physics programming software design photographer writer artist artisan tradition pottery woodwork kimono fashion furniture architecture housing store market company office law tax economy politics city town village tokyo osaka kyoto hokkaido okinawa personal hobby club association map walking ramen soba sushi theater rakugo go shogi calligraphy poetry novel translation language dictionary archive data`.split(' ');
const zJa = zipf(JA.length), zEn = zipf(EN.length);
const jaSent = n => { let s = ''; for (let i = 0; i < n; i++) { s += JA[zJa()] + (rnd() < 0.3 ? 'の' : rnd() < 0.5 ? 'と' : ''); if (rnd() < 0.05) s += pick(RARE); } return s + 'を紹介するサイトです。'; };
const enSent = n => Array.from({ length: n }, () => EN[zEn()]).join(' ') + '.';
// タグ語彙 2000（日本語名・英語名）。上位はよく使われる
const TAGS = Array.from({ length: 2000 }, (_, i) => i < JA.length ? { label_ja: JA[i], label_en: EN[i % EN.length] } : { label_ja: JA[i % JA.length] + JA[(i * 7) % JA.length] + (i % 10), label_en: `tag${i}` });
const zTag = zipf(TAGS.length, 1.0);
const cats = db.prepare(`SELECT c.id FROM categories c WHERE NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = c.id) AND c.parent_id IS NOT 'government' ORDER BY sort_order`).all().map(r => r.id);
const zCat = zipf(cats.length, 0.9);
const CC = ['JP', 'JP', 'JP', 'JP', 'JP', 'JP', 'JP', 'US', 'US', 'GB', 'DE', 'FR', 'KR', 'TW', 'CA', 'AU', 'IT', 'ES', 'NL', 'SE', 'BR', 'IN', 'SG', 'NZ', 'CH', 'FI'];
const have = db.prepare('SELECT COUNT(*) n FROM sites').get().n;
const insS = db.prepare('INSERT INTO sites (id,url,name,description_ja,description_en,country_code,region_code,source_updated_at,registered_at,bulk_import,search_text) VALUES (?,?,?,?,?,?,?,?,?,?,?)');
const insT = db.prepare('INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)');
const insC = db.prepare('INSERT OR IGNORE INTO site_categories (site_id,category_id) VALUES (?,?)');
db.exec('BEGIN');
for (let i = 0; i < target - have; i++) {
  const id = `syn-${String(i).padStart(6, '0')}`;
  const host = Array.from({ length: 6 + Math.floor(rnd() * 8) }, () => pick('abcdefghijklmnopqrstuvwxyz')).join('') + i.toString(36);
  const cc = pick(CC), url = `https://${host}.${cc === 'JP' ? pick(['jp', 'com', 'net', 'co.jp']) : pick(['com', 'org', 'net'])}/`;
  const name = rnd() < 0.6 ? JA[zJa()] + pick(['工房', 'の部屋', '研究所', 'ブログ', '堂', '屋', '日記', 'ラボ', '']) + (rnd() < 0.3 ? ' ' + EN[zEn()] : '') : EN[zEn()] + ' ' + EN[zEn()] + ' ' + pick(['Studio', 'Lab', 'Works', 'Journal', 'Club']);
  const dj = jaSent(4 + Math.floor(rnd() * 10)), de = rnd() < 0.7 ? enSent(8 + Math.floor(rnd() * 12)) : null;
  const inBulk = PROD_SHAPE && i >= Math.floor((target - have) * 0.02) && i < Math.floor((target - have) * 0.88);
  const reg = inBulk ? '2026-11-13 00:00:00' : new Date(Date.UTC(2026, 10, 15) - Math.floor(rnd() * 1095) * 86400000 - Math.floor(rnd() * 86400) * 1000).toISOString().replace(/\.\d+Z$/, 'Z');
  const upd = inBulk ? null : rnd() < 0.4 ? new Date(Date.UTC(2026, 10, 15) - Math.floor(rnd() * 2000) * 86400000).toISOString().slice(0, 10) : null;
  const bulk = PROD_SHAPE ? (inBulk ? 1 : 0) : rnd() < 0.15 ? 1 : 0;
  const tags = []; const nt = Math.floor(rnd() * 6); for (let k = 0; k < nt; k++) { const t = TAGS[zTag()]; if (!tags.some(x => x.label_ja === t.label_ja)) tags.push(t); }
  tags.sort((a, b) => a.label_ja < b.label_ja ? -1 : a.label_ja > b.label_ja ? 1 : 0);
  const rec = { name, description_ja: dj, description_en: de, url, tags };
  insS.run(id, url, name, dj, de, cc, null, upd, reg, bulk, searchText(rec));
  for (const t of tags) insT.run(id, t.label_ja, t.label_en);
  const nc = 1 + Math.floor(rnd() * 3); for (let k = 0; k < nc; k++) insC.run(id, cats[zCat()]);
}
db.exec('COMMIT');
// タグのトリガが search_text を ASCII 小文字版で上書きしているので、合成行は NFKC 版に戻す（refresh-search-text.mjs と同じ結果）
const rows = db.prepare(`SELECT id,name,description_ja,description_en,url FROM sites WHERE id LIKE 'syn-%'`).all();
const tagsBy = new Map(); for (const t of db.prepare(`SELECT site_id,label_ja,label_en FROM site_tags WHERE site_id LIKE 'syn-%' ORDER BY site_id,label_ja`).all()) { if (!tagsBy.has(t.site_id)) tagsBy.set(t.site_id, []); tagsBy.get(t.site_id).push(t); }
const up = db.prepare('UPDATE sites SET search_text=? WHERE id=?');
db.exec('BEGIN'); for (const r of rows) up.run(searchText({ ...r, tags: tagsBy.get(r.id) ?? [] }), r.id); db.exec('COMMIT');
db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
console.log(db.prepare('SELECT COUNT(*) n, (SELECT COUNT(*) FROM site_tags) t, (SELECT COUNT(*) FROM site_categories) c, avg(length(search_text)) l FROM sites').get());
