/**
 * 读者契约正文的**三方一致**测试：
 *
 *   `plugin/contracts/reader-contract-v4.zh.txt`
 *   == `src/contract-text.ts` 导出的 `READER_CONTRACT_V4`
 *   == `docs/12-读者契约v4.md` §2 代码块（逐字，含标点与换行）
 *
 * 提取失败一律抛错（路径、小节、代码块围栏都写进错误信息），**不允许静默跳过**：
 * 契约是定稿文本，`skip` 会让「一致」变成空头承诺。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  READER_CONTRACT_PATH,
  READER_CONTRACT_RELATIVE_PATH,
  READER_CONTRACT_V4,
  READER_CONTRACT_VERSION,
  normalizeContractText,
  readContractText,
} from '../src/contract-text.js';

const DOC_PATH = fileURLToPath(
  new URL('../../docs/12-读者契约v4.md', import.meta.url),
);
const PACKAGE_PATH = fileURLToPath(new URL('../package.json', import.meta.url));

/** 从 docs/12 §2 提取契约代码块（围栏内容，不含围栏行）。 */
function docContractBlock(): string {
  let raw: string;
  try {
    raw = readFileSync(DOC_PATH, 'utf8');
  } catch (error) {
    throw new Error(
      `三方一致测试无法读取 docs/12：${DOC_PATH}。` +
        '契约正文的比对基准就是这份文档，缺少它无法断言一致（不会跳过）。',
      { cause: error },
    );
  }
  const lines = raw.replace(/^\uFEFF/, '').split(/\r?\n/);

  const heading = lines.findIndex((line) => /^##\s+2\./.test(line));
  if (heading < 0) {
    throw new Error(
      `docs/12 里找不到 "## 2." 小节，无法定位契约代码块：${DOC_PATH}`,
    );
  }

  let open = -1;
  for (let index = heading + 1; index < lines.length; index += 1) {
    if (/^##\s+/.test(lines[index])) break; // 已离开 §2
    if (/^```/.test(lines[index])) {
      open = index;
      break;
    }
  }
  if (open < 0) {
    throw new Error(
      `docs/12 §2 的标题之后找不到 \`\`\` 代码块起始行：${DOC_PATH}`,
    );
  }
  if (lines[open].trim() !== '```') {
    throw new Error(
      `docs/12 §2 的代码块起始行不是裸 \`\`\`，而是 ${JSON.stringify(lines[open])}：${DOC_PATH}`,
    );
  }

  let close = -1;
  for (let index = open + 1; index < lines.length; index += 1) {
    if (/^```/.test(lines[index])) {
      close = index;
      break;
    }
  }
  if (close < 0) {
    throw new Error(`docs/12 §2 的代码块没有闭合的 \`\`\`：${DOC_PATH}`);
  }

  const body = lines.slice(open + 1, close);
  if (body.length === 0) {
    throw new Error(`docs/12 §2 的代码块为空：${DOC_PATH}`);
  }
  return body.join('\n');
}

function readPackageJson(): {
  name: string;
  files: string[];
  exports: Record<string, unknown>;
} {
  return JSON.parse(readFileSync(PACKAGE_PATH, 'utf8')) as {
    name: string;
    files: string[];
    exports: Record<string, unknown>;
  };
}

describe('读者契约正文（单一来源）', () => {
  it('能从 docs/12 §2 提取代码块（提取失败必须抛错，不能跳过）', () => {
    const block = docContractBlock();
    expect(block.length).toBeGreaterThan(400);
    expect(block.startsWith('回答这些关于我本人记忆的问题时，请遵守：')).toBe(
      true,
    );
    expect(block.endsWith('同一件事的补记不算新增。')).toBe(true);
  });

  it('contracts/*.txt 与 docs/12 §2 逐字一致', () => {
    const raw = readFileSync(READER_CONTRACT_PATH, 'utf8');
    expect(normalizeContractText(raw)).toBe(docContractBlock());
  });

  it('contract-text.ts 的导出值与 contracts/*.txt 一致', () => {
    expect(READER_CONTRACT_V4).toBe(readContractText());
    expect(READER_CONTRACT_V4).toBe(
      normalizeContractText(readFileSync(READER_CONTRACT_PATH, 'utf8')),
    );
  });

  it('contract-text.ts 的导出值与 docs/12 §2 逐字一致', () => {
    expect(READER_CONTRACT_V4).toBe(docContractBlock());
    expect(READER_CONTRACT_V4).toHaveLength(docContractBlock().length);
  });

  it('正文文件是稳定的 UTF-8 LF + 单个结尾换行（防止编辑器/autocrlf 改写正文）', () => {
    const raw = readFileSync(READER_CONTRACT_PATH, 'utf8');
    expect(raw).not.toContain('\r');
    expect(raw.startsWith('\uFEFF')).toBe(false);
    expect(raw.endsWith('\n')).toBe(true);
    expect(raw.endsWith('\n\n')).toBe(false);
    // 归一化只去掉结尾那一个换行，不改正文本身。
    expect(normalizeContractText(raw)).toBe(raw.slice(0, -1));
  });

  it('版本号与文件路径自洽', () => {
    expect(READER_CONTRACT_VERSION).toBe('v4');
    expect(READER_CONTRACT_RELATIVE_PATH).toBe(
      'contracts/reader-contract-v4.zh.txt',
    );
    // Windows 的 fileURLToPath 给反斜杠，比较前统一成 URL 风格的分隔符。
    expect(
      READER_CONTRACT_PATH.replace(/\\/g, '/').endsWith(
        READER_CONTRACT_RELATIVE_PATH,
      ),
    ).toBe(true);
  });

  it('npm 打包会带上正文（package.json 的 files 含 contracts）', () => {
    const pkg = readPackageJson();
    expect(pkg.files).toContain('contracts');
  });

  it('正文缺失时抛带路径的清晰错误，而不是返回空串', () => {
    const missing = fileURLToPath(
      new URL('../contracts/does-not-exist.zh.txt', import.meta.url),
    );
    expect(() => readContractText(missing)).toThrow(
      /无法读取读者契约正文/,
    );
    expect(() => readContractText(missing)).toThrow(missing);
  });
});
