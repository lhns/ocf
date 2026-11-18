import sys, re
name, root = sys.argv[1], sys.argv[2]
def sub(path, old, new):
    p = root + '/src/' + path
    s = open(p).read()
    assert s.count(old) == 1, (name, path, old, s.count(old))
    open(p, 'w').write(s.replace(old, new))

if name == 'flag_clear_on_completion':
    sub('ocf_core.c', 'env_atomic64_inc(&core->flush_skip_done);', 'env_atomic64_set(&core->flush_skip_done, 1);')
    sub('ocf_core.c', 'done == flushed) {', 'done == 0) {')
    sub('ocf_core.c', '\tuint64_t cur = env_atomic64_read(&core->flush_skip_flushed);', '\tenv_atomic64_set(&core->flush_skip_done, 0);\n\treturn;\n\tuint64_t cur = env_atomic64_read(&core->flush_skip_flushed);')
elif name == 'flag_clear_on_issue':
    sub('ocf_core.c', 'env_atomic64_inc(&core->flush_skip_done);', 'env_atomic64_set(&core->flush_skip_done, 1);')
    sub('ocf_core.c', 'done == flushed) {', 'done == 0) {')
    sub('ocf_core.c', '\tenv_atomic64_inc(&cache->flushes_forwarded);\n\treturn false;', '\tenv_atomic64_inc(&cache->flushes_forwarded);\n\tenv_atomic64_set(&core->flush_skip_done, 0);\n\treturn false;')
elif name == 'seq_cutoff_uncounted':
    sub('ocf_request.c', 'if (req->core_write_counted) {\n\t\t\tocf_core_flush_skip_write_done', 'if (req->core_write_counted && !req->seq_cutoff) {\n\t\t\tocf_core_flush_skip_write_done')
elif name == 'discard_uncounted':
    sub('ocf_request.c', 'req->core_write_counted = !req->cleaner;', 'req->core_write_counted = false;')
elif name == 'flushed_on_failure':
    sub('ocf_request.c', 'if (!error)\n\t\t\t\tocf_core_flush_skip_flushed', 'if (1)\n\t\t\t\tocf_core_flush_skip_flushed')
elif name == 'always_skip':
    sub('ocf_core.c', 'env_atomic_read(&cache->flush_skip_enabled) && done == flushed', 'env_atomic_read(&cache->flush_skip_enabled)')
elif name == 'cleaner_counted':
    sub('ocf_request.c', 'req->core_write_counted = (dir == OCF_WRITE) && !req->cleaner;', 'req->core_write_counted = (dir == OCF_WRITE);')
elif name == 'done_at_submit':
    sub('ocf_request.c', 'req->core_write_counted = (dir == OCF_WRITE) && !req->cleaner;', 'req->core_write_counted = false;\n\tif (dir == OCF_WRITE && !req->cleaner)\n\t\tocf_core_flush_skip_write_done(req->core);')
    sub('ocf_request.c', 'req->core_write_counted = !req->cleaner;', 'req->core_write_counted = false;\n\tif (!req->cleaner)\n\t\tocf_core_flush_skip_write_done(req->core);')
elif name == 'flushed_no_max':
    sub('ocf_core.c', '\twhile (cur < gen) {', '\tif (1) {\n\t\tenv_atomic64_set(&core->flush_skip_flushed, gen);\n\t\treturn;\n\t}\n\twhile (cur < gen) {')
else:
    raise SystemExit('unknown ' + name)
