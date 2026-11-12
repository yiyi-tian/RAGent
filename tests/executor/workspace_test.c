#define _GNU_SOURCE
#include "executor/workspace.h"
#include "protocol/protocol.h"

#include <assert.h>
#include <stdbool.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static json_object *call(int root, const char *tool, const char *arguments, bool ok)
{
    json_object *request = json_object_new_object();
    json_object_object_add(request, "tool", json_object_new_string(tool));
    json_object *args = json_parse_object((const unsigned char *)arguments, strlen(arguments));
    assert(args != NULL);
    json_object_object_add(request, "args", args);
    json_object *result = workspace_execute(root, request);
    assert(result != NULL);
    if (json_object_get_boolean(json_object_object_get(result, "ok")) != ok) {
        fprintf(stderr, "%s %s: %s\n", tool, arguments, json_object_to_json_string(result));
        abort();
    }
    json_object_put(request);
    return result;
}

static void check(int root, const char *tool, const char *args, bool ok)
{
    json_object_put(call(root, tool, args, ok));
}

int main(void)
{
    char path[] = "/tmp/ragent-workspace-XXXXXX";
    assert(mkdtemp(path) != NULL);
    int root = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    assert(root >= 0);
    check(root, "workspace_mkdir", "{\"path\":\"notes\"}", true);
    check(root, "workspace_write", "{\"path\":\"notes/a.txt\",\"content\":\"你好\"}", true);
    check(root, "workspace_write", "{\"path\":\"notes/a.txt\",\"content\":\"bad\"}", false);
    json_object *read = call(root, "workspace_read", "{\"path\":\"notes/a.txt\"}", true);
    assert(strcmp(json_object_get_string(json_object_object_get(read, "output")), "你好") == 0);
    json_object_put(read);
    check(root, "workspace_copy", "{\"source\":\"notes/a.txt\",\"destination\":\"b.txt\"}", true);
    check(root, "workspace_move", "{\"source\":\"b.txt\",\"destination\":\"c.txt\"}", true);
    check(root, "workspace_move", "{\"source\":\"c.txt\",\"destination\":\"notes/a.txt\"}", false);
    check(root, "workspace_remove", "{\"path\":\"notes\"}", false);
    check(root, "workspace_remove", "{\"path\":\".\"}", false);
    const char *denied[] = {
        "{\"path\":\"/etc/passwd\"}",
        "{\"path\":\"../outside\"}",
        "{\"path\":\"notes/../../outside\"}",
        "{\"path\":\"c.txt\\u0000other\"}",
        "{\"path\":\".ragent-private\"}",
    };
    for (size_t i = 0; i < sizeof(denied) / sizeof(denied[0]); ++i) {
        check(root, "workspace_read", denied[i], false);
    }
    assert(symlinkat("/etc", root, "escape") == 0);
    check(root, "workspace_read", "{\"path\":\"escape/passwd\"}", false);
    check(root, "workspace_write", "{\"path\":\"escape/x\",\"content\":\"bad\"}", false);
    assert(unlinkat(root, "escape", 0) == 0);
    assert(linkat(root, "c.txt", root, "hard", 0) == 0);
    check(root, "workspace_read", "{\"path\":\"hard\"}", false);
    assert(unlinkat(root, "hard", 0) == 0);
    check(root, "workspace_remove", "{\"path\":\"c.txt\"}", true);
    check(root, "workspace_remove", "{\"path\":\"notes/a.txt\"}", true);
    check(root, "workspace_remove", "{\"path\":\"notes\"}", true);
    for (int i = 0; i < 128; ++i) {
        char args[128];
        snprintf(args, sizeof(args), "{\"path\":\"f%03d\",\"content\":\"x\"}", i);
        check(root, "workspace_write", args, true);
    }
    check(root, "workspace_write", "{\"path\":\"overflow\",\"content\":\"x\"}", false);
    json_object *listing = call(root, "workspace_list", "{\"path\":\".\"}", true);
    const char *text = json_object_get_string(json_object_object_get(listing, "output"));
    json_object *page = json_parse_object((const unsigned char *)text, strlen(text));
    assert(json_object_array_length(json_object_object_get(page, "entries")) == 32);
    assert(json_object_get_int(json_object_object_get(page, "next_offset")) == 32);
    json_object_put(page);
    json_object_put(listing);
    for (int i = 0; i < 128; ++i) {
        char name[32];
        snprintf(name, sizeof(name), "f%03d", i);
        assert(unlinkat(root, name, 0) == 0);
    }
    close(root);
    assert(rmdir(path) == 0);
    return 0;
}
