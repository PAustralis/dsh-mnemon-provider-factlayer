/**
 * 包入口（docs/06 §1）：default export 一个 `defineMemorySpaceProvider({ id, apply })`
 * 的 typed child module。
 *
 * 装配逻辑在 `entry.ts`（`loadEntryModule`）：`defineMemorySpaceProvider` 通过动态
 * import 取得（见 `define.ts`），本包不把它装成依赖，peer 由宿主 profile 提供。若宿主
 * 没装，模块加载会以「需要安装 dsh-mnemon」的清晰错误结束，而不是装配出一个半残
 * provider。
 *
 * 顶层 await 说明：动态 import 必须在**模块求值期**完成，才能让 default export
 * 真的经过 `defineMemorySpaceProvider` 包装；Node >= 20 的 ESM loader 原生支持。
 */

import { loadEntryModule } from './entry.js';

const entryModule = await loadEntryModule();

export default entryModule;

export { createFactlayerAdapter, createFactlayerModule } from './module.js';
export { FACTLAYER_MANIFEST, PROVIDER_ID } from './manifest.js';
