import { Hono } from 'hono';
const app = new Hono();
app.get('/api/categories', async c => {
  const { results } = await c.env.DB.prepare(`WITH RECURSIVE tree(root_id, id) AS (SELECT id, id FROM categories UNION ALL SELECT tree.root_id, c.id FROM tree JOIN categories c ON c.parent_id = tree.id) SELECT c.id, c.parent_id, c.name_ja, c.name_en, c.sort_order, COUNT(DISTINCT sc.site_id) AS site_count FROM categories c LEFT JOIN tree ON tree.root_id = c.id LEFT JOIN site_categories sc ON sc.category_id = tree.id GROUP BY c.id ORDER BY c.sort_order`).all();
  return c.json(results);
});
app.get('/api/sites', async c => {
  const { results } = await c.env.DB.prepare(`SELECT s.id, s.name, s.url, s.description_ja, s.favicon_path, s.registered_at, s.last_crawled_at, (SELECT json_group_array(json_object('ja',t.label_ja,'en',t.label_en)) FROM site_tags t WHERE t.site_id=s.id) AS tags_json, sc.category_id FROM sites s JOIN site_categories sc ON s.id = sc.site_id ORDER BY s.id`).all();
  return c.json(results);
});
app.get('/api/activity', async c => {
  const totals = await c.env.DB.prepare(`SELECT COUNT(*) AS total, SUM(CASE WHEN registered_at IS NULL OR datetime(registered_at) IS NULL OR datetime(registered_at) > datetime('now') THEN 1 ELSE 0 END) AS unknown_dates FROM sites`).first();
  const { results } = await c.env.DB.prepare(`SELECT date(registered_at) AS day, COUNT(*) AS count FROM sites WHERE datetime(registered_at) IS NOT NULL AND datetime(registered_at) <= datetime('now') AND date(registered_at) >= date('now','-6 days') GROUP BY date(registered_at) ORDER BY day`).all();
  return c.json({total:totals.total, unknown_dates:totals.unknown_dates || 0, known_daily:results});
});
export default app;
