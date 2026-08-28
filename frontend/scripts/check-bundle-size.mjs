import { gzipSync } from 'node:zlib';
import { readFile, readdir } from 'node:fs/promises';
import { extname, join, relative } from 'node:path';

const DIST_DIR = new URL('../dist/', import.meta.url);
const LIMITS = {
  entry: 180 * 1024,
  javascript: 300 * 1024,
  stylesheet: 140 * 1024,
  initialJavaScriptGzip: 220 * 1024,
};

async function listFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = await Promise.all(
    entries.map((entry) => {
      const path = join(directory, entry.name);
      return entry.isDirectory() ? listFiles(path) : [path];
    })
  );
  return files.flat();
}

function formatBytes(bytes) {
  return `${(bytes / 1024).toFixed(2)} KiB`;
}

const manifestPath = new URL('.vite/manifest.json', DIST_DIR);
let manifest;
try {
  manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
} catch (error) {
  console.error('Bundle manifest is missing. Run `npm run build` before the size gate.');
  throw error;
}

const entry = Object.values(manifest).find((item) => item.isEntry);
if (!entry) throw new Error('Bundle manifest does not contain an application entry.');

const initialFiles = new Set();
function collectInitialFiles(item) {
  if (!item || initialFiles.has(item.file)) return;
  initialFiles.add(item.file);
  for (const importedKey of item.imports || []) collectInitialFiles(manifest[importedKey]);
}
collectInitialFiles(entry);

const distPath = new URL('.', DIST_DIR).pathname;
const files = (await listFiles(distPath)).filter((path) => ['.css', '.js'].includes(extname(path)));
const failures = [];
const measurements = [];
let initialJavaScriptGzip = 0;

for (const path of files) {
  const content = await readFile(path);
  const name = relative(distPath, path);
  const extension = extname(path);
  const bytes = content.byteLength;
  const gzipBytes = gzipSync(content).byteLength;
  const limit = extension === '.js' ? LIMITS.javascript : LIMITS.stylesheet;
  if (bytes > limit) failures.push(`${name}: ${formatBytes(bytes)} > ${formatBytes(limit)}`);
  if (name === entry.file && bytes > LIMITS.entry) {
    failures.push(`${name} (entry): ${formatBytes(bytes)} > ${formatBytes(LIMITS.entry)}`);
  }
  if (extension === '.js' && initialFiles.has(name)) initialJavaScriptGzip += gzipBytes;
  measurements.push({ name, bytes, gzipBytes });
}

if (initialJavaScriptGzip > LIMITS.initialJavaScriptGzip) {
  failures.push(
    `initial JavaScript gzip: ${formatBytes(initialJavaScriptGzip)} > ${formatBytes(LIMITS.initialJavaScriptGzip)}`
  );
}

measurements
  .sort((left, right) => right.bytes - left.bytes)
  .forEach(({ name, bytes, gzipBytes }) => {
    console.log(
      `${name.padEnd(44)} ${formatBytes(bytes).padStart(12)} gzip ${formatBytes(gzipBytes)}`
    );
  });
console.log(`Initial JavaScript gzip: ${formatBytes(initialJavaScriptGzip)}`);

if (failures.length) {
  console.error('\nBundle size gate failed:');
  failures.forEach((failure) => console.error(`- ${failure}`));
  process.exitCode = 1;
} else {
  console.log('Bundle size gate passed.');
}
