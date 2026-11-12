/**
 * 读者契约 v4 的**唯一正文来源**：包内 `contracts/reader-contract-v4.zh.txt`
 * （逐字抄自 `docs/12-读者契约v4.md` §2 的代码块）。
 *
 * 为什么运行时读文件、而不是在 TS 里再内嵌一份：
 * - 单一来源。契约改版时只改 `.txt`，任何内嵌副本都可能忘了同步；
 * - 本文件同时被 bundle 的 prompt-section 插件（`contract.ts` → `./contract`）
 *   读取，构建产物 `dist/contract-text.js` 与源码 `src/contract-text.ts`
 *   都位于包根下一级，`../contracts/...` 相对 `import.meta.url` 指向同一路径；
 * - npm 打包后仍然可用：`package.json` 的 `files` 必须含 `contracts`
 *   （`tests/contract-text.test.ts` 会断言这一条，防止发包把正文漏掉）。
 *
 * 代价：多一个「文件缺失」失败点，所以读失败时抛带绝对路径与修复提示的错误，
 * 而不是静默返回空串（空契约会悄悄改变读者行为，比崩溃更难发现）。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/** 契约版本号；改正文必须同时改这里，并重跑 A–E 对照（docs/12 §5）。 */
export const READER_CONTRACT_VERSION = 'v4' as const;

/** 包内相对路径（相对包根），也是 npm 打包时 `files` 要包含的目录。 */
export const READER_CONTRACT_RELATIVE_PATH = 'contracts/reader-contract-v4.zh.txt';

/**
 * 正文文件的绝对路径。
 *
 * `src/` 与 `dist/` 都在包根下一级，所以同一个 `../contracts/...` 在源码运行
 * （vitest）和构建产物运行（宿主 loader）里都指向包根的 `contracts/`。
 */
export const READER_CONTRACT_PATH = fileURLToPath(
  new URL(`../${READER_CONTRACT_RELATIVE_PATH}`, import.meta.url),
);

/**
 * 把文件原文归一成「可比较、可注入」的正文：
 * 去 BOM、CRLF → LF、去掉恰好一个结尾换行（文件按 POSIX 习惯以换行结尾，
 * 但系统提示的段文本不应以空行收尾）。
 * @param raw - 文件原文。
 * @returns 归一后的契约正文。
 */
export function normalizeContractText(raw: string): string {
  const unified = raw.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n');
  return unified.endsWith('\n') ? unified.slice(0, -1) : unified;
}

/**
 * 读取并校验正文文件。
 * @param path - 正文文件绝对路径；默认 {@link READER_CONTRACT_PATH}。
 * @returns 归一后的契约正文；空文件或读取失败都抛错（不返回空串）。
 */
export function readContractText(path: string = READER_CONTRACT_PATH): string {
  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch (error) {
    throw new Error(
      `无法读取读者契约正文：${path}。` +
        '该文件随包交付（package.json 的 files 必须包含 "contracts"）；' +
        '若在源码树里运行，请确认 plugin/contracts/reader-contract-v4.zh.txt 存在且已提交。',
      { cause: error },
    );
  }
  const text = normalizeContractText(raw);
  if (text.trim() === '') {
    throw new Error(
      `读者契约正文为空：${path}。` +
        `期望逐字等于 docs/12-读者契约v4.md §2 的代码块（${READER_CONTRACT_VERSION}）。`,
    );
  }
  return text;
}

/** 读者契约 v4 正文（模块加载时读取一次）。 */
export const READER_CONTRACT_V4: string = readContractText();
