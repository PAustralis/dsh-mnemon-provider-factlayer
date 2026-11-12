/**
 * 入口装配（docs/06 §1）：先取 Provider SDK，再用它把 typed child module 包成
 * 真正的 default export。
 *
 * 为什么抽成函数而不全写在 `index.ts`：本仓库为让本包在 profile 里解析 peer，
 * 故意保留 `node_modules/dsh-mnemon*` 两个 junction（生产需要），所以入口的裸
 * 动态 import 永远成功，「SDK 缺失」分支无法靠 import 真实触发。这里开放一个
 * importer 注入点：测试喂一个解析必然失败的 importer，就能真走
 * `loadDefineMemorySpaceProvider` 的 catch 与 `ProviderSdkUnavailableError`；
 * 同时仍可对真实环境断言「装配成功且 id 正确」。
 */

import type { DefineMemorySpaceProvider, MemorySpaceProviderModule } from './contract.js';
import {
  defaultSdkModuleImporter,
  loadDefineMemorySpaceProvider,
  type SdkModuleImporter,
} from './define.js';
import { createFactlayerModule } from './module.js';

/** 入口的模块求值结果：`defineMemorySpaceProvider(createFactlayerModule())`。 */
export async function loadEntryModule(
  importModule: SdkModuleImporter = defaultSdkModuleImporter,
): Promise<MemorySpaceProviderModule> {
  const defineMemorySpaceProvider: DefineMemorySpaceProvider =
    await loadDefineMemorySpaceProvider(importModule);
  return defineMemorySpaceProvider(createFactlayerModule());
}
