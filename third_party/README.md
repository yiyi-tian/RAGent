# 固定命令解析依赖

- tree-sitter：v0.25.10，保留上游lib/include、lib/src及MIT许可证。
- tree-sitter-bash：v0.25.1，保留上游生成的C解析器、扫描器及MIT许可证。

来源为对应GitHub官方仓库的版本归档。日常CMake构建不下载依赖，也不需要Node.js生成解析器。
上游代码保持原样；策略规则由项目自己的C模块维护。
