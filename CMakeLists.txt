cmake_minimum_required(VERSION 3.16)
project(RAGent VERSION 0.1.0 LANGUAGES C)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "RAGent currently requires Linux")
endif()

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(CURL REQUIRED IMPORTED_TARGET libcurl)
pkg_check_modules(JSONC REQUIRED IMPORTED_TARGET json-c)

add_library(agent-common src/protocol/protocol.c src/net/socket.c)
target_include_directories(agent-common PUBLIC "${PROJECT_SOURCE_DIR}/include")
target_link_libraries(agent-common PUBLIC PkgConfig::JSONC)
target_compile_options(agent-common PRIVATE -Wall -Wextra -Wpedantic)

add_executable(agent-server src/server/main.c src/server/worker_pool.c src/server/agent_http.c)
target_link_libraries(agent-server PRIVATE Threads::Threads PkgConfig::CURL)
add_executable(agent src/client/main.c)
foreach(target agent-server agent)
    target_link_libraries(${target} PRIVATE agent-common)
    target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
endforeach()

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_executable(agent-server-test src/server/main.c src/server/worker_pool.c src/server/agent_http.c)
    target_compile_definitions(agent-server-test PRIVATE RAGENT_TESTING)
    target_link_libraries(agent-server-test PRIVATE agent-common Threads::Threads PkgConfig::CURL)
    target_compile_options(agent-server-test PRIVATE -Wall -Wextra -Wpedantic)
    add_executable(worker-pool-test tests/worker_pool_test.c src/server/worker_pool.c src/server/agent_http.c)
    target_compile_definitions(worker-pool-test PRIVATE RAGENT_TESTING)
    target_link_libraries(worker-pool-test PRIVATE agent-common Threads::Threads PkgConfig::CURL)
    target_compile_options(worker-pool-test PRIVATE -Wall -Wextra -Wpedantic)
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
endif()

if(BUILD_TESTING)
    execute_process(COMMAND ${Python3_EXECUTABLE} -c "import ragent, rag, tools, aiohttp, langchain_core, langchain_openai"
        RESULT_VARIABLE AGENT_DEPS_STATUS OUTPUT_QUIET ERROR_QUIET)
    if(AGENT_DEPS_STATUS EQUAL 0)
        add_test(NAME agent-unit COMMAND ${Python3_EXECUTABLE} -m unittest discover -s tests/agent -v)
        set_tests_properties(agent-unit PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
            ENVIRONMENT "RAGENT_SERVER=$<TARGET_FILE:agent-server>" TIMEOUT 60
            RUN_SERIAL TRUE)
    else()
        message(STATUS "Agent tests disabled: run pip install -e '.[dev]' in the selected Python environment")
    endif()
endif()
