cmake_minimum_required(VERSION 3.16)
project(RAGent VERSION 0.1.0 LANGUAGES C)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "RAGent currently requires Linux")
endif()

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
find_package(PkgConfig REQUIRED)
pkg_check_modules(JSONC REQUIRED IMPORTED_TARGET json-c)

add_library(agent-common src/protocol/protocol.c src/protocol/tool_validate.c src/net/socket.c)
target_include_directories(agent-common PUBLIC "${PROJECT_SOURCE_DIR}/include")
target_link_libraries(agent-common PUBLIC PkgConfig::JSONC)
target_compile_options(agent-common PRIVATE -Wall -Wextra -Wpedantic)

add_executable(agent-server src/server/main.c)
add_executable(agent src/client/main.c)
foreach(target agent-server agent)
    target_link_libraries(${target} PRIVATE agent-common)
    target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
endforeach()

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_test(NAME network-integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration/network_test.py
                $<TARGET_FILE:agent-server> $<TARGET_FILE:agent>)
    set_tests_properties(network-integration PROPERTIES TIMEOUT 90)
endif()
