# Single source of truth for security blocklists.
# Imported by both utils/hooks.py (Python fallback path) and
# hooks/pre_tool_use.py (subprocess path) so they always enforce the same rules.

BLOCKED_TOOL_NAMES = {
    'sqlite-tools__write_query',
    'sqlite-tools__create_table',
    'sqlite-tools__append_insight',
}

BLOCKED_PATH_PATTERNS = ['..', '/etc', '/bin', '/usr', '/sbin', '/var', '/proc', '/sys']

BLOCKED_COMMANDS = [
    'rm ', 'rm\t', 'mkfs', 'dd ', 'chmod 777', 'curl ', 'wget ',
    'create table', 'drop table', 'drop database', 'insert into',
    'delete from', 'truncate ', 'alter table', 'update ',
]
