cmake_minimum_required(VERSION 3.16)
project(RAGent VERSION 0.1.0 LANGUAGES CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR "RAGent currently requires Linux")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(Boost 1.75 REQUIRED COMPONENTS json)

add_library(agent-common src/protocol/protocol.cpp src/net/socket.cpp)
target_include_directories(agent-common PUBLIC include)
target_link_libraries(agent-common PUBLIC Boost::json)
target_compile_options(agent-common PRIVATE -Wall -Wextra -Wpedantic)

add_executable(agent-server src/server/main.cpp)
add_executable(agent src/client/main.cpp)
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
