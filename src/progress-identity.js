// Same rule as scripts/progress_identity.py and migration 0034.
export function progressIdentityKey(value) {
  const input = String(value ?? '').trim();
  const match = input.match(/^(https?):\/\/([^/?#]*)([^#]*)(?:#.*)?$/i);
  if (!match) throw new TypeError('invalid progress URL');
  const [, rawScheme, rawAuthority] = match;
  let authority = rawAuthority.toLowerCase();
  if (!authority || authority.includes('@')) throw new TypeError('invalid progress URL');
  if (authority.startsWith('www.')) authority = authority.slice(4);
  const scheme = rawScheme.toLowerCase();
  if ((scheme === 'https' && authority.endsWith(':443')) || (scheme === 'http' && authority.endsWith(':80'))) authority = authority.slice(0, authority.lastIndexOf(':'));
  let pathAndQuery = match[3];
  if (!pathAndQuery || pathAndQuery.startsWith('?')) pathAndQuery = '/' + pathAndQuery;
  // Validate syntax/protocol without using the URL serializer, which could silently fold path spelling.
  try { const u = new URL(input); if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) throw new Error(); }
  catch { throw new TypeError('invalid progress URL'); }
  return authority + pathAndQuery;
}
