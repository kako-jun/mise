// Match machigai-salad: generate the build date in JST, not the request date.
import { writeFile } from 'node:fs/promises';
const date = new Date(Date.now() + 9 * 60 * 60 * 1000).toISOString().slice(0, 10);
await writeFile(new URL('../public/build-date.json', import.meta.url), JSON.stringify({ date }) + '\n');
