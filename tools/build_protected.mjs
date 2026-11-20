// 把工作台各页打包成「不可读」的对外产物：HTML 压缩 + CSS 压缩 + JS 压缩混淆
// 用法: node build_protected.mjs <源目录> <输出目录> [文件...]
import fs from 'fs';
import path from 'path';
import { minify } from 'terser';
import JavaScriptObfuscator from 'javascript-obfuscator';

const [, , srcDir, outDir, ...only] = process.argv;
if (!srcDir || !outDir) { console.error('usage: node build_protected.mjs <src> <out> [files]'); process.exit(2); }

const FILES = only.length ? only : ['workbench.html', 'dash-c2.html', 'kpi-c2.html', 'safety.html', 'index.html'];

const CSS_MIN = (css) => css
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/\s*([{}:;,>~+])\s*/g, '$1')
  .replace(/;}/g, '}')
  .replace(/\s+/g, ' ')
  .trim();

const jsOpts = {
  compact: true,
  controlFlowFlattening: false,
  deadCodeInjection: false,
  identifierNamesGenerator: 'hexadecimal',
  numbersToExpressions: true,
  simplify: true,
  splitStrings: true,
  splitStringsChunkLength: 6,
  stringArray: true,
  stringArrayEncoding: ['base64'],
  stringArrayThreshold: 0.75,
  stringArrayRotate: true,
  stringArrayShuffle: true,
  transformObjectKeys: false,
  unicodeEscapeSequence: false,
  selfDefending: false,
  disableConsoleOutput: false,
};

async function transform(html, name) {
  const scripts = [];
  const styles = [];
  // 抽出 script / style 块（先占位，处理完再回填，避免正则互相干扰）
  let out = html.replace(/(<script\b[^>]*>)([\s\S]*?)(<\/script>)/gi, (m, a, body, c) => {
    if (/\bsrc\s*=/.test(a)) return m;
    if (!body.trim()) return m;
    const i = scripts.length; scripts.push(body);
    return `\u0000S${i}\u0000`;
  });
  out = out.replace(/(<style\b[^>]*>)([\s\S]*?)(<\/style>)/gi, (m, a, body, c) => {
    const i = styles.length; styles.push(CSS_MIN(body));
    return `\u0000C${i}\u0000`;
  });
  // 去 HTML 注释
  out = out.replace(/<!--(?!\[if)[\s\S]*?-->/g, '');
  // 折叠标签间空白
  out = out.replace(/>\s+</g, '><').replace(/\n{2,}/g, '\n').trim();

  const t0 = Date.now();
  const outScripts = [];
  for (const s of scripts) {
    let code = s;
    try {
      const r1 = await minify(code, { compress: { passes: 2 }, mangle: true, format: { comments: false } });
      if (r1 && r1.code) code = r1.code;
    } catch (e) {
      console.error(`  ! terser ${name}: ${e.message}`);
    }
    try {
      code = JavaScriptObfuscator.obfuscate(code, jsOpts).getObfuscatedCode();
    } catch (e) {
      console.error(`  ! obfuscator ${name}: ${e.message}`);
    }
    outScripts.push(code);
  }
  out = out.replace(/\u0000S(\d+)\u0000/g, (m, i) => `<script>${outScripts[+i]}</script>`);
  out = out.replace(/\u0000C(\d+)\u0000/g, (m, i) => `<style>${styles[+i]}</style>`);
  console.error(`  ${name}: js ${scripts.length} 段, css ${styles.length} 段, ${Date.now() - t0}ms`);
  return out;
}

fs.mkdirSync(outDir, { recursive: true });
for (const f of FILES) {
  const p = path.join(srcDir, f);
  if (!fs.existsSync(p)) { console.error('  skip(缺文件) ' + f); continue; }
  const html = fs.readFileSync(p, 'utf8');
  const res = await transform(html, f);
  fs.writeFileSync(path.join(outDir, f), res, 'utf8');
  console.log(`${f}: ${(html.length / 1024).toFixed(1)}KB → ${(res.length / 1024).toFixed(1)}KB`);
}
