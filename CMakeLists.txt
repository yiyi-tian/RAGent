cmake_minimum_required(VERSION 3.16)
project(RAGent VERSION 0.1.0 LANGUAGES C)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "RAGent currently requires Linux")
endif()

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
# 缺失函数声明会在64位平台截断返回指针，必须在构建时失败。
add_compile_options(-Werror=implicit-function-declaration)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(CURL REQUIRED IMPORTED_TARGET libcurl)
pkg_check_modules(JSONC REQUIRED IMPORTED_TARGET json-c)

add_library(agent-common src/protocol/protocol.c src/net/socket.c)
target_include_directories(agent-common PUBLIC "${PROJECT_SOURCE_DIR}/include")
target_link_libraries(agent-common PUBLIC PkgConfig::JSONC)
target_compile_options(agent-common PRIVATE -Wall -Wextra -Wpedantic)

add_executable(agent-server src/server/main.c src/server/worker_pool.c src/server/agent_http.c
    src/server/task_bridge.c)
target_link_libraries(agent-server PRIVATE Threads::Threads PkgConfig::CURL)
add_executable(agent src/client/main.c)
foreach(target agent-server agent)
    target_link_libraries(${target} PRIVATE agent-common)
    target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
endforeach()

add_library(command-runner src/executor/command.c)
target_include_directories(command-runner PUBLIC "${PROJECT_SOURCE_DIR}/include")
target_compile_options(command-runner PRIVATE -Wall -Wextra -Wpedantic)
add_executable(ragent-executor src/executor/main.c)
target_link_libraries(ragent-executor PRIVATE command-runner agent-common)
target_compile_options(ragent-executor PRIVATE -Wall -Wextra -Wpedantic)

add_library(workspace-files src/executor/workspace.c)
target_include_directories(workspace-files PUBLIC "${PROJECT_SOURCE_DIR}/include")
target_link_libraries(workspace-files PUBLIC agent-common)
target_compile_options(workspace-files PRIVATE -Wall -Wextra -Wpedantic)
add_executable(ragent-file-worker src/executor/file_main.c)
target_link_libraries(ragent-file-worker PRIVATE workspace-files)
target_compile_options(ragent-file-worker PRIVATE -Wall -Wextra -Wpedantic)

add_executable(ragent-sandbox src/executor/sandbox.c)
target_link_libraries(ragent-sandbox PRIVATE command-runner agent-common)
target_compile_options(ragent-sandbox PRIVATE -Wall -Wextra -Wpedantic)

add_library(shell-parser STATIC
    third_party/tree-sitter/lib/src/lib.c
    third_party/tree-sitter-bash/src/parser.c
    third_party/tree-sitter-bash/src/scanner.c)
target_include_directories(shell-parser PUBLIC third_party/tree-sitter/lib/include
    PRIVATE third_party/tree-sitter/lib/src third_party/tree-sitter-bash/src)
target_compile_definitions(shell-parser PRIVATE _DEFAULT_SOURCE)
add_library(command-policy src/executor/policy.c)
target_include_directories(command-policy PUBLIC include)
target_link_libraries(command-policy PUBLIC shell-parser agent-common)
target_compile_options(command-policy PRIVATE -Wall -Wextra -Wpedantic)
add_library(readonly-observer src/executor/observe.c)
target_link_libraries(readonly-observer PUBLIC command-policy)
target_compile_options(readonly-observer PRIVATE -Wall -Wextra -Wpedantic)
add_executable(ragent-observe src/executor/observe_main.c)
target_link_libraries(ragent-observe PRIVATE readonly-observer)
target_compile_options(ragent-observe PRIVATE -Wall -Wextra -Wpedantic)
add_executable(ragent-sni-check src/executor/sni_check.c)
target_compile_options(ragent-sni-check PRIVATE -Wall -Wextra -Wpedantic)
add_library(command-owner src/executor/owner.c)
target_include_directories(command-owner PUBLIC include)
target_link_libraries(command-owner PUBLIC agent-common)
target_compile_options(command-owner PRIVATE -Wall -Wextra -Wpedantic)
add_executable(ragent-reaper src/executor/reaper.c)
target_link_libraries(ragent-reaper PRIVATE command-owner command-runner)
target_compile_options(ragent-reaper PRIVATE -Wall -Wextra -Wpedantic)
add_library(agent-terminal src/client/terminal.c)
target_include_directories(agent-terminal PUBLIC include)
target_compile_options(agent-terminal PRIVATE -Wall -Wextra -Wpedantic)
add_library(command-network src/executor/network.c)
target_link_libraries(command-network PUBLIC command-owner command-runner)
target_compile_options(command-network PRIVATE -Wall -Wextra -Wpedantic)
add_library(command-authorization src/executor/authorization.c)
target_link_libraries(command-authorization PUBLIC command-network agent-terminal)
target_compile_options(command-authorization PRIVATE -Wall -Wextra -Wpedantic)
add_executable(ragent-launcher src/executor/launcher.c)
target_link_libraries(ragent-launcher PRIVATE agent-common)
target_compile_options(ragent-launcher PRIVATE -Wall -Wextra -Wpedantic)
add_library(command-container src/executor/container.c)
target_link_libraries(command-container PUBLIC command-policy command-runner command-owner command-network command-authorization)
target_compile_options(command-container PRIVATE -Wall -Wextra -Wpedantic)
add_library(local-commands src/executor/local.c)
target_link_libraries(local-commands PUBLIC command-policy command-runner command-container agent-terminal)
target_link_libraries(agent PRIVATE local-commands)
target_compile_options(local-commands PRIVATE -Wall -Wextra -Wpedantic)
add_executable(ragent-policy src/executor/policy_main.c)
target_link_libraries(ragent-policy PRIVATE command-policy)
target_compile_options(ragent-policy PRIVATE -Wall -Wextra -Wpedantic)

include(CTest)
if(BUILD_TESTING)
    add_executable(container-fixture tests/executor/container_fixture.c)
    target_link_libraries(container-fixture PRIVATE command-container)
    target_compile_options(container-fixture PRIVATE -Wall -Wextra -Wpedantic)
    add_executable(observe-boundary-test tests/executor/observe_boundary_test.c)
    target_link_libraries(observe-boundary-test PRIVATE readonly-observer)
    target_compile_options(observe-boundary-test PRIVATE -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME observe-boundary-unit COMMAND observe-boundary-test)
    set_tests_properties(observe-boundary-unit PROPERTIES TIMEOUT 15)
    add_executable(policy-test tests/executor/policy_test.c)
    target_link_libraries(policy-test PRIVATE command-policy)
    target_compile_options(policy-test PRIVATE -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME command-policy-unit COMMAND policy-test)
    set_tests_properties(command-policy-unit PROPERTIES TIMEOUT 15)
    add_executable(workspace-test tests/executor/workspace_test.c)
    target_link_libraries(workspace-test PRIVATE workspace-files)
    target_compile_options(workspace-test PRIVATE -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME workspace-unit COMMAND workspace-test)
    set_tests_properties(workspace-unit PROPERTIES TIMEOUT 15)
    add_executable(executor-test tests/executor/runner_test.c)
    target_link_libraries(executor-test PRIVATE command-runner)
    target_compile_options(executor-test PRIVATE -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME executor-unit COMMAND executor-test)
    set_tests_properties(executor-unit PROPERTIES TIMEOUT 15)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_test(NAME command-container-integration COMMAND ${Python3_EXECUTABLE}
        ${PROJECT_SOURCE_DIR}/tests/executor/container_test.py $<TARGET_FILE:container-fixture>)
    set_tests_properties(command-container-integration PROPERTIES TIMEOUT 120 RUN_SERIAL TRUE)
    add_test(NAME observe-integration COMMAND ${Python3_EXECUTABLE}
        ${PROJECT_SOURCE_DIR}/tests/executor/observe_test.py $<TARGET_FILE:ragent-observe>)
    set_tests_properties(observe-integration PROPERTIES TIMEOUT 30)
    add_executable(socket-probe tools/socket_probe.c)
    target_link_libraries(socket-probe PRIVATE Threads::Threads PkgConfig::JSONC)
    target_compile_options(socket-probe PRIVATE -Wall -Wextra -Wpedantic)
    add_test(NAME native-probe-unit COMMAND ${Python3_EXECUTABLE}
        ${PROJECT_SOURCE_DIR}/tests/benchmark/native_probe_test.py $<TARGET_FILE:socket-probe>)
    set_tests_properties(native-probe-unit PROPERTIES TIMEOUT 45)
    add_executable(agent-server-test src/server/main.c src/server/worker_pool.c src/server/agent_http.c
    src/server/task_bridge.c)
    target_compile_definitions(agent-server-test PRIVATE RAGENT_TESTING)
    target_link_libraries(agent-server-test PRIVATE agent-common Threads::Threads PkgConfig::CURL)
    target_compile_options(agent-server-test PRIVATE -Wall -Wextra -Wpedantic)
    add_executable(worker-pool-test tests/worker_pool_test.c src/server/worker_pool.c src/server/agent_http.c)
    target_compile_definitions(worker-pool-test PRIVATE RAGENT_TESTING)
    target_link_libraries(worker-pool-test PRIVATE agent-common Threads::Threads PkgConfig::CURL)
    # Release 构建中测试仍需执行 assert，不能让 NDEBUG 移除检查和测试调用。
    target_compile_options(worker-pool-test PRIVATE -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME worker-pool-backpressure COMMAND worker-pool-test)
    set_tests_properties(worker-pool-backpressure PROPERTIES TIMEOUT 10)
    add_test(NAME deployment-unit
        COMMAND ${Python3_EXECUTABLE} -m unittest discover -s tests/deployment -v)
    set_tests_properties(deployment-unit PROPERTIES
        WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR} TIMEOUT 60)
    add_test(NAME rag-unit
        COMMAND ${Python3_EXECUTABLE} -m unittest discover -s tests/rag -v)
    set_tests_properties(rag-unit PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR} TIMEOUT 30)
    add_test(NAME concurrency-integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration/concurrency_test.py
                $<TARGET_FILE:agent-server-test> $<TARGET_FILE:agent>)
    set_tests_properties(concurrency-integration PROPERTIES TIMEOUT 30)
    add_test(NAME network-integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration/network_test.py
                $<TARGET_FILE:agent-server> $<TARGET_FILE:agent>)
    set_tests_properties(network-integration PROPERTIES TIMEOUT 90)

    add_executable(network-grant-test tests/executor/network_test.c)
    target_link_libraries(network-grant-test PRIVATE command-network)
    target_compile_options(network-grant-test PRIVATE -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME network-grant-unit COMMAND network-grant-test)
    add_test(NAME sni-helper-unit COMMAND ${Python3_EXECUTABLE}
        ${PROJECT_SOURCE_DIR}/tests/executor/sni_test.py $<TARGET_FILE:ragent-sni-check>)

    add_executable(terminal-test tests/client/terminal_test.c)
    target_link_libraries(terminal-test PRIVATE agent-terminal)
    target_compile_options(terminal-test PRIVATE -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME terminal-unit COMMAND terminal-test)

endif()

if(BUILD_TESTING)
    execute_process(
        COMMAND ${Python3_EXECUTABLE} -c
            "import ragent, rag, tools, aiohttp, uvloop, langchain_core, langchain_openai"
        RESULT_VARIABLE AGENT_DEPS_STATUS OUTPUT_QUIET ERROR_QUIET)
    if(AGENT_DEPS_STATUS EQUAL 0)
        add_test(NAME agent-workers-integration COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration/agent_workers_test.py)
        set_tests_properties(agent-workers-integration PROPERTIES
            WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR} TIMEOUT 180 RUN_SERIAL TRUE
            ENVIRONMENT "RAGENT_EXECUTOR=$<TARGET_FILE:ragent-executor>")
        add_test(NAME agent-unit COMMAND ${Python3_EXECUTABLE} -m unittest discover -s tests/agent -v)
        set_tests_properties(agent-unit PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
            ENVIRONMENT "RAGENT_SERVER=$<TARGET_FILE:agent-server>;RAGENT_EXECUTOR=$<TARGET_FILE:ragent-executor>"
            TIMEOUT 90
            RUN_SERIAL TRUE)
    else()
        message(STATUS "Agent tests disabled: run pip install -e '.[dev]' in the selected Python environment")
    endif()
endif()

install(TARGETS agent ragent-observe ragent-policy ragent-reaper
    RUNTIME DESTINATION libexec/ragent)
