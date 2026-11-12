/**
 * 读者契约 v4 的 **prompt-section 插件**（包子路径 `./contract`，构建为
 * `dist/contract-plugin.js`）。
 *
 * 生效链路（docs/12 §4.1 / docs/13 §1.5）：
 *   profile `package.json` 的 `dsh.profile.bundles` 加本包名
 *   → boot 读本包 `package.json` 的 `dsh.bundle.patch` = `./cordis.patch.yml`
 *   → patch 插入一行 `name: dsh-mnemon-provider-factlayer/contract`
 *   → 本模块 `inject: ['systemPrompt']`，`apply` 注册一个全局提示词段。
 *
 * 只加一段全局 section：不碰 persona（`deployment:persona-*`），不碰 preset。
 * 段名与 persona 段不同名 → 并存而非覆盖；order 10250 排在 persona suffix
 * （10200）之后，外部贡献允许任意有限 order。
 *
 * 为什么不叫 `contract.ts`：本包的 `src/contract.ts` 已经是 provider 契约的
 * **类型镜像**（5 个 src 模块 + 4 个测试 import 它），改名会牵连整个包；
 * 对外子路径仍是 `./contract`，文件名差异不影响装载。
 *
 * 注意：不要为了类型把 `@deepseek-ai/dsh-system-prompt` 写进 `peerDependencies`
 * ——`@deepseek-ai/dsh-*` 前缀会进 DSH 的插件兼容检查。这里用结构类型。
 */

import { READER_CONTRACT_V4 } from './contract-text.js';

/** 全局段名：与 `deployment:persona-prefix` / `deployment:persona-suffix` 不同名，并存。 */
export const READER_CONTRACT_SECTION_NAME = 'mnemon:reader-contract';

/** 段顺序：10200（persona suffix）之后；harness 工具段（1000–5000）与源码段（10000）之后。 */
export const READER_CONTRACT_SECTION_ORDER = 10250;

/** `ctx.systemPrompt.section()` 的入参（`dsh-system-prompt` 的结构镜像）。 */
export interface PromptSectionLike {
  name: string;
  order: number;
  text: string;
}

/** `ctx.systemPrompt` 的最小结构镜像（只声明本插件真正调用的方法）。 */
export interface SystemPromptLike {
  section(section: PromptSectionLike): () => void;
}

/** 本插件要求的宿主上下文。 */
export interface ReaderContractContext {
  systemPrompt: SystemPromptLike;
}

/** 等宿主提供 `systemPrompt` 服务后才 apply。 */
export const inject = ['systemPrompt'];

/**
 * 注册读者契约段（每步装配时进入系统提示）。
 * @param ctx - 提供 `ctx.systemPrompt` 的 Host 上下文。
 */
export function apply(ctx: ReaderContractContext): void {
  ctx.systemPrompt.section({
    name: READER_CONTRACT_SECTION_NAME,
    order: READER_CONTRACT_SECTION_ORDER,
    text: READER_CONTRACT_V4,
  });
}
