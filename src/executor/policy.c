#define _POSIX_C_SOURCE 200809L
#include "executor/policy.h"

#include <errno.h>
#include <fcntl.h>
#include <pwd.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdbool.h>
#include <fnmatch.h>
#include <stdlib.h>
#include <string.h>
#include <tree_sitter/api.h>

extern const TSLanguage *tree_sitter_bash(void);

typedef struct {
    const char *source;
    int level;
    unsigned commands;
    json_object *risks;
    json_object *programs;
} Analysis;

static void risk(Analysis *analysis, int level, const char *reason)
{
    if (level > analysis->level) {
        analysis->level = level;
    }
    size_t count = json_object_array_length(analysis->risks);
    for (size_t index = 0; index < count; ++index) {
        const char *existing
            = json_object_get_string(json_object_array_get_idx(analysis->risks, index));
        if (strcmp(existing, reason) == 0) {
            return;
        }
    }
    json_object_array_add(analysis->risks, json_object_new_string(reason));
}

static bool type_is(TSNode node, const char *type)
{
    return strcmp(ts_node_type(node), type) == 0;
}

/* 这里只还原静态引号；任何展开都不能通过此函数进入宿主机执行计划。 */
static char *literal(TSNode node, const char *source)
{
    if (ts_node_is_null(node)) {
        return NULL;
    }
    const char *type = ts_node_type(node);
    if (strcmp(type, "command_name") == 0) {
        return literal(ts_node_named_child(node, 0), source);
    }
    if (strcmp(type, "word") != 0 && strcmp(type, "raw_string") != 0 && strcmp(type, "string") != 0
        && strcmp(type, "number") != 0) {
        return NULL;
    }
    uint32_t children = ts_node_named_child_count(node);
    for (uint32_t index = 0; index < children; ++index) {
        if (!type_is(ts_node_named_child(node, index), "string_content")) {
            return NULL;
        }
    }
    size_t start = ts_node_start_byte(node);
    size_t end = ts_node_end_byte(node);
    bool single = strcmp(type, "raw_string") == 0;
    bool quoted = single || strcmp(type, "string") == 0;
    if (quoted) {
        ++start;
        --end;
    }
    char *text = malloc(end - start + 1);
    if (text == NULL) {
        return NULL;
    }
    size_t written = 0;
    for (size_t index = start; index < end; ++index) {
        char value = source[index];
        if (!single && value == '\\' && index + 1 < end) {
            char next = source[index + 1];
            if (!quoted || next == '$' || next == '`' || next == '"' || next == '\\'
                || next == '\n') {
                ++index;
                if (next == '\n') {
                    continue;
                }
                value = next;
            }
        } else if (!quoted
            && (value == '$' || value == '`' || value == '~' || value == '{' || value == '}')) {
            free(text);
            return NULL;
        }
        text[written++] = value;
    }
    text[written] = '\0';
    return text;
}

static bool in_list(const char *value, const char *const list[])
{
    for (size_t index = 0; list[index] != NULL; ++index) {
        if (strcmp(value, list[index]) == 0) {
            return true;
        }
    }
    return false;
}

const char *const *policy_observer_names(void)
{
    static const char *const names[] = {
        "ls",
        "pwd",
        "cat",
        "head",
        "tail",
        "grep",
        "find",
        "echo",
        "printf",
        "wc",
        "git",
        NULL,
    };
    return names;
}

static bool protected_config_invalid;

static int load_protected_names(const char **patterns, size_t used)
{
    struct passwd *account = getpwuid(getuid());
    char path[4096];
    if (account == NULL
        || snprintf(path, sizeof(path), "%s/.config/ragent/protected-names", account->pw_dir)
            >= (int)sizeof(path)) {
        return -1;
    }
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        return errno == ENOENT ? 0 : -1;
    }
    struct stat info;
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid()
        || (info.st_mode & 0022) != 0 || info.st_size > 8192) {
        close(fd);
        return -1;
    }
    FILE *file = fdopen(fd, "r");
    if (file == NULL) {
        close(fd);
        return -1;
    }
    char line[256];
    int status = 0;
    while (fgets(line, sizeof(line), file) != NULL) {
        size_t length = strlen(line);
        if (length == 0 || (line[length - 1] != '\n' && !feof(file))) {
            status = -1;
            break;
        }
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }
        if (used >= 63 || strlen(line) > 128 || strchr(line, '/') != NULL) {
            status = -1;
            break;
        }
        for (const unsigned char *byte = (const unsigned char *)line; *byte != 0; ++byte) {
            if (*byte < 32 || *byte == 127) {
                status = -1;
            }
        }
        if (status != 0 || (patterns[used] = strdup(line)) == NULL) {
            status = -1;
            break;
        }
        ++used;
    }
    if (ferror(file)) {
        status = -1;
    }
    if (fclose(file) != 0) {
        status = -1;
    }
    return status;
}

const char *const *policy_protected_names(void)
{
    static const char *patterns[64] = {
        ".env*",
        "*.pem",
        "*.key",
        "id_rsa*",
        "id_ed25519*",
        ".netrc",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        ".ssh",
        ".aws",
        ".azure",
        ".gnupg",
        ".docker",
        ".config",
        ".local",
        "credentials",
        NULL,
    };
    static bool loaded;
    if (!loaded) {
        size_t used = 0;
        while (patterns[used] != NULL) {
            ++used;
        }
        protected_config_invalid = load_protected_names(patterns, used) != 0;
        loaded = true;
    }
    return patterns;
}

bool policy_protected_path(const char *text)
{
    const char *assignment = strchr(text, '=');
    if (assignment != NULL) {
        text = assignment + 1;
    }
    char *copy = strdup(text);
    if (copy == NULL) {
        return true;
    }
    const char *const *patterns = policy_protected_names();
    bool protected = false;
    char *context = NULL;
    for (char *part = strtok_r(copy, "/", &context); part != NULL && !protected;
         part = strtok_r(NULL, "/", &context)) {
        for (size_t index = 0; patterns[index] != NULL; ++index) {
            if (fnmatch(patterns[index], part, 0) == 0) {
                protected = true;
                break;
            }
        }
    }
    free(copy);
    return protected;
}

static bool safe_substitution(const char *script)
{
    if (script[0] != 's' || script[1] == '\0') {
        return false;
    }
    char separator = script[1];
    if (separator == '\\' || separator == '\n' || separator == '\r') {
        return false;
    }
    unsigned sections = 0;
    const char *cursor = script + 2;
    for (; *cursor != '\0' && sections < 2; ++cursor) {
        if (*cursor == '\\' && cursor[1] != '\0') {
            ++cursor;
        } else if (*cursor == separator) {
            ++sections;
        } else if (*cursor == '\n' || *cursor == '\r') {
            return false;
        }
    }
    if (sections != 2) {
        return false;
    }
    for (; *cursor != '\0'; ++cursor) {
        if (strchr("gIiMm0123456789", *cursor) == NULL) {
            return false;
        }
    }
    return true;
}

static void inspect_command(TSNode node, Analysis *analysis)
{
    char *words[128] = { 0 };
    size_t count = 0;
    uint32_t children = ts_node_named_child_count(node);
    bool static_arguments = true;
    for (uint32_t index = 0; index < children; ++index) {
        TSNode child = ts_node_named_child(node, index);
        if (type_is(child, "file_redirect")) {
            continue;
        }
        if (count == 128 || (words[count] = literal(child, analysis->source)) == NULL) {
            static_arguments = false;
            break;
        }
        ++count;
    }
    if (++analysis->commands > 32 || !static_arguments || count == 0) {
        risk(analysis, 3, "包含动态命令、展开、赋值或尚未支持的参数语法");
        goto cleanup;
    }
    const char *program = words[0];
    if (strncmp(program, "/usr/bin/", 9) == 0) {
        program += 9;
    } else if (strncmp(program, "/bin/", 5) == 0) {
        program += 5;
    }
    const char *const *observers = policy_observer_names();
    const char *const writers[] = {
        "touch",
        "mkdir",
        "rm",
        "rmdir",
        "mv",
        "cp",
        NULL,
    };
    const char *const network[] = {
        "curl",
        "wget",
        "pip",
        "pip3",
        "npm",
        "npx",
        "ssh",
        "scp",
        "sftp",
        NULL,
    };
    if (in_list(program, network)) {
        risk(analysis, 3, "可能访问外部网络、引入第三方代码或改变外部系统");
    } else if (in_list(program, writers)) {
        risk(analysis, 2, "会修改挂载工作区；变更会反映到宿主机，不自动回滚");
    } else if (strcmp(program, "sed") == 0) {
        if (count >= 4 && strncmp(words[1], "-i", 2) == 0 && safe_substitution(words[2])) {
            risk(analysis, 2, "sed原地替换会修改工作区文件，不自动回滚");
        } else {
            risk(analysis, 3, "sed脚本可能写文件或执行命令，尚不能证明其完整行为");
        }
    } else if (strcmp(program, "git") == 0) {
        if (count < 2
            || (strcmp(words[1], "status") != 0 && strcmp(words[1], "log") != 0
                && strcmp(words[1], "diff") != 0)) {
            risk(analysis, 3, "Git操作可能变更仓库、调用外部程序或访问网络");
        } else {
            for (size_t index = 2; index < count; ++index) {
                const char *argument = words[index];
                if (strncmp(argument, "--", 2) == 0 && strcmp(argument, "--stat") != 0
                    && strcmp(argument, "--oneline") != 0 && strcmp(argument, "--short") != 0
                    && strcmp(argument, "--branch") != 0 && strcmp(argument, "--cached") != 0
                    && strcmp(argument, "--staged") != 0
                    && strncmp(argument, "--porcelain", 11) != 0 && strcmp(argument, "--") != 0) {
                    risk(analysis, 3, "Git参数不在已验证的观察选项内");
                }
                if (argument[0] == '-' && argument[1] != '-' && strcmp(argument, "-s") != 0
                    && strcmp(argument, "-b") != 0 && strncmp(argument, "-n", 2) != 0) {
                    risk(analysis, 3, "Git参数可能启用补丁、外部程序或其他动态行为");
                }
            }
        }
    } else if (!in_list(program, observers)) {
        risk(analysis, 3, "解释器、项目代码或未知程序无法静态证明为只读");
    }
    for (size_t index = 1; index < count; ++index) {
        if (policy_protected_path(words[index])) {
            risk(analysis, 3, "涉及凭证或受保护文件，需单独授权");
        }
        if (strcmp(program, "find") == 0
            && (strncmp(words[index], "-exec", 5) == 0 || strncmp(words[index], "-ok", 3) == 0
                || strcmp(words[index], "-delete") == 0 || strncmp(words[index], "-fprint", 7) == 0
                || strcmp(words[index], "-fprintf") == 0 || strcmp(words[index], "-fls") == 0)) {
            bool executes
                = strncmp(words[index], "-exec", 5) == 0 || strncmp(words[index], "-ok", 3) == 0;
            risk(analysis, executes ? 3 : 2, "find包含删除、文件输出或执行其他程序的动作");
        }
        if (strcmp(program, "printf") == 0 && strcmp(words[index], "-v") == 0) {
            risk(analysis, 3, "包含Shell内建赋值参数");
        }
    }
    json_object *entry = json_object_new_object();
    json_object *arguments = json_object_new_array();
    for (size_t index = 0; index < count; ++index) {
        json_object_array_add(arguments, json_object_new_string(words[index]));
    }
    json_object_object_add(entry, "argv", arguments);
    json_object_object_add(entry, "start", json_object_new_int64(ts_node_start_byte(node)));
    json_object_object_add(entry, "end", json_object_new_int64(ts_node_end_byte(node)));
    json_object_array_add(analysis->programs, entry);
cleanup:
    for (size_t index = 0; index < count; ++index) {
        free(words[index]);
    }
}

static void inspect_node(TSNode node, Analysis *analysis, unsigned depth)
{
    if (depth > 128) {
        risk(analysis, 3, "语法嵌套超过分析上限");
        return;
    }
    const char *type = ts_node_type(node);
    if (strcmp(type, "command") == 0) {
        inspect_command(node, analysis);
    } else if (strcmp(type, "file_redirect") == 0) {
        const char *start = analysis->source + ts_node_start_byte(node);
        size_t length = ts_node_end_byte(node) - ts_node_start_byte(node);
        if (memchr(start, '>', length) != NULL) {
            risk(analysis, 2, "包含文件输出重定向，可能覆盖或追加工作区文件");
        } else {
            risk(analysis, 3, "输入重定向须由容器解释，不能直接交给宿主机Shell");
        }
    } else if (strcmp(type, "program") != 0 && strcmp(type, "list") != 0
        && strcmp(type, "pipeline") != 0 && strcmp(type, "subshell") != 0
        && strcmp(type, "redirected_statement") != 0 && strcmp(type, "comment") != 0
        && strcmp(type, "word") != 0 && strcmp(type, "number") != 0
        && strcmp(type, "command_name") != 0 && strcmp(type, "string") != 0
        && strcmp(type, "raw_string") != 0 && strcmp(type, "string_content") != 0) {
        risk(analysis, 3, "包含解释器展开、控制结构或无法静态确定的语法");
    }
    uint32_t children = ts_node_named_child_count(node);
    for (uint32_t index = 0; index < children; ++index) {
        inspect_node(ts_node_named_child(node, index), analysis, depth + 1);
    }
    uint32_t tokens = ts_node_child_count(node);
    for (uint32_t index = 0; index < tokens; ++index) {
        TSNode child = ts_node_child(node, index);
        if (!ts_node_is_named(child) && type_is(child, "&")) {
            risk(analysis, 3, "后台执行无法按普通观察命令同步管理");
        }
    }
}

json_object *command_policy(const char *command)
{
    Analysis analysis = {
        .source = command,
        .level = 1,
        .risks = json_object_new_array(),
        .programs = json_object_new_array(),
    };
    TSParser *parser = ts_parser_new();
    TSTree *tree = NULL;
    const char *denial = NULL;
    size_t length = strlen(command);
    policy_protected_names();
    if (protected_config_invalid) {
        denial = "本机敏感文件配置非法，未执行；请检查~/.config/ragent/protected-names";
    } else if (length == 0 || length > POLICY_COMMAND_LIMIT) {
        denial = "命令为空或超过16 KiB限制";
    } else if (parser == NULL || !ts_parser_set_language(parser, tree_sitter_bash())) {
        denial = "命令语法分析器不可用";
    } else {
        tree = ts_parser_parse_string(parser, NULL, command, (uint32_t)length);
        if (tree == NULL || ts_node_has_error(ts_tree_root_node(tree))) {
            denial = "命令存在语法错误，未执行";
        } else {
            inspect_node(ts_tree_root_node(tree), &analysis, 0);
            if (analysis.commands == 0) {
                denial = "命令不包含可执行步骤";
            }
        }
    }
    if (denial != NULL) {
        risk(&analysis, 3, denial);
    }
    json_object *result = json_object_new_object();
    json_object_object_add(result, "level", json_object_new_int(analysis.level));
    json_object_object_add(
        result, "requires_confirmation", json_object_new_boolean(analysis.level != 1));
    json_object_object_add(result,
        "environment",
        json_object_new_string(analysis.level == 1 ? "host_readonly" : "container"));
    json_object_object_add(result, "risks", analysis.risks);
    json_object_object_add(result, "programs", analysis.programs);
    json_object_object_add(
        result, "denial_reason", denial == NULL ? NULL : json_object_new_string(denial));
    ts_tree_delete(tree);
    ts_parser_delete(parser);
    return result;
}
