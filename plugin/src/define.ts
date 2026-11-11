/**
 * 运行期取 `defineMemorySpaceProvider`（docs/06 §1，B1 装配形状）。
 *
 * 06 要求子模块 default export 一个 `defineMemorySpaceProvider({ id, apply })`；
 * 本包**不在编译期依赖** dsh-mnemon（peer 由宿主 profile 提供，仓库里不装），
 * 所以运行期动态 import，失败时抛出带「需要安装 dsh-mnemon」的清晰错误。
 *
 * 已核实的包布局（本机 `dsh-mnemon@0.5.37`）：
 * - `defineMemorySpaceProvider` 由 **`dsh-mnemon-source-memory-spaces/provider-sdk`**
 *   导出（官方 9 个 provider 全是 `import ... from "dsh-mnemon-source-memory-spaces/provider-sdk"`）；
 * - `dsh-mnemon` 主入口**没有**重新导出它（grep 无命中）。
 *
 * 因此按 `dsh-mnemon-source-memory-spaces/provider-sdk` → `dsh-mnemon` 顺序探测：
 * 已核实前者是真实导出面，先试它可避免每次装配都白 evaluate 一遍
 * `dsh-mnemon` 主入口的 core；后者保留为回退（宿主若将来重新导出也能命中）。
 */

import type { DefineMemorySpaceProvider } from './contract.js';

/** 探测顺序；第一个是 0.5.37 的实际导出面，第二个是 06/任务的装配要求（保留回退）。 */
const SDK_CANDIDATES: string[] = [
  'dsh-mnemon-source-memory-spaces/provider-sdk',
  'dsh-mnemon',
];

export class ProviderSdkUnavailableError extends Error {
  readonly attempts: string[];

  constructor(attempts: string[]) {
    super(
      '需要安装 dsh-mnemon（其 Provider SDK 提供 defineMemorySpaceProvider 才能装配 factlayer provider）。'
        + `已尝试：${attempts.join('；')}`,
    );
    this.name = 'ProviderSdkUnavailableError';
    this.attempts = attempts;
  }
}

export async function loadDefineMemorySpaceProvider(): Promise<DefineMemorySpaceProvider> {
  const attempts: string[] = [];
  for (const specifier of SDK_CANDIDATES) {
    try {
      // 用变量做 specifier：TypeScript 不会在编译期解析它（本包没有装 peer）。
      const loaded = (await import(specifier)) as Record<string, unknown>;
      const fromNamespace = loaded.defineMemorySpaceProvider;
      const defaultExport = loaded.default;
      const fromDefault =
        typeof defaultExport === 'object' && defaultExport !== null
          ? (defaultExport as Record<string, unknown>).defineMemorySpaceProvider
          : undefined;
      const candidate = fromNamespace ?? fromDefault;
      if (typeof candidate === 'function') {
        return candidate as DefineMemorySpaceProvider;
      }
      attempts.push(`${specifier}：未导出 defineMemorySpaceProvider`);
    } catch (error) {
      attempts.push(`${specifier}：${error instanceof Error ? error.message : String(error)}`);
    }
  }
  throw new ProviderSdkUnavailableError(attempts);
}
