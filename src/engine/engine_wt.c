/*
 * Copyright(c) 2012-2022 Intel Corporation
 * Copyright(c) 2024 Huawei Technologies
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "ocf/ocf.h"
#include "../ocf_cache_priv.h"
#include "engine_wt.h"
#include "engine_wi.h"
#include "engine_inv.h"
#include "engine_common.h"
#include "engine_io.h"
#include "../ocf_request.h"
#include "../utils/utils_cache_line.h"
#include "../utils/utils_user_part.h"
#include "../metadata/metadata.h"
#include "../concurrency/ocf_concurrency.h"

#define OCF_ENGINE_DEBUG_IO_NAME "wt"
#include "engine_debug.h"

static void _ocf_write_wt_update_bits(struct ocf_request *req)
{
	bool miss = ocf_engine_is_miss(req);
	bool repart = ocf_engine_needs_repart(req);

	if (!miss && !repart)
		return;

	ocf_hb_req_prot_lock_wr(req);

	if (miss) {
		/* Update valid status bits */
		ocf_set_valid_map_info(req);
	}

	if (repart) {
		OCF_DEBUG_RQ(req, "Re-Part");
		/* Probably some cache lines are assigned into wrong
		 * partition. Need to move it to new one
		 */
		ocf_user_part_move(req);
	}

	ocf_hb_req_prot_unlock_wr(req);
}

static void _ocf_write_wt_req_complete(struct ocf_request *req)
{
	if (env_atomic_dec_return(&req->req_remaining))
		return;

	OCF_DEBUG_RQ(req, "Completion");

	if (req->info.cache_error || req->info.core_error) {
		if (req->info.cache_error && req->info.dirty_any &&
				!req->error) {
			/* Dirty sectors of the request are kept (see below),
			 * but their cache copy may be torn now, so the write
			 * must not be acknowledged. */
			req->error = -OCF_ERR_IO;
		}
		req->complete(req, req->error);
		/* Dirty sectors hold the only durable copy of the data
		 * written earlier, so they must not be purged. */
		ocf_engine_invalidate_keep_dirty(req);
		return;
	}

	ocf_req_unlock_wr(ocf_cache_line_concurrency(req->cache), req);
	req->complete(req, 0);
	ocf_req_put(req);
}

static void _ocf_write_wt_cache_complete(struct ocf_request *req, int error)
{
	if (error) {
		/* Cache error code is not propagated further to the user here
		 * because data could be successfully written to the core device
		 * despite the cache IO error.
		 * Error flag is set though to indicate that the error occurred
		 * and to invalidate the request in completion. */
		req->info.cache_error = 1;
		ocf_core_stats_cache_error_update(req->core, OCF_WRITE);
		inc_fallback_pt_error_counter(req->cache);
	}

	_ocf_write_wt_req_complete(req);
}

static void _ocf_write_wt_core_complete(struct ocf_request *req, int error)
{
	if (error) {
		req->info.core_error = 1;
		req->error = error;
		ocf_core_stats_core_error_update(req->core, OCF_WRITE);
	}

	_ocf_write_wt_req_complete(req);
}

static inline void _ocf_write_wt_submit(struct ocf_request *req)
{
	/* Submit IOs */
	OCF_DEBUG_RQ(req, "Submit");

	env_atomic_set(&req->req_remaining, 2); /* cache IO + core IO */

	/* To cache */
	ocf_engine_forward_cache_io_req(req, OCF_WRITE,
			_ocf_write_wt_cache_complete);

	/* To core */
	ocf_engine_forward_core_io_req(req, _ocf_write_wt_core_complete);
}

static int _ocf_write_wt_do(struct ocf_request *req)
{
	/* Get OCF request - increase reference counter */
	ocf_req_get(req);

	/* Sectors that are dirty stay dirty: both the cache and the core get
	 * the new data, but the core copy is not durable until flushed, and
	 * dirty status is what protects the data after a crash. The cleaner
	 * will rewrite the same data to the core and flush it before cleaning.
	 */
	_ocf_write_wt_update_bits(req);
	ENV_BUG_ON(req->info.flush_metadata);

	/* Submit IO */
	_ocf_write_wt_submit(req);

	/* Update statistics */
	ocf_engine_update_request_stats(req);
	ocf_engine_update_block_stats(req);

	/* Put OCF request - decrease reference counter */
	ocf_req_put(req);

	return 0;
}

static const struct ocf_engine_callbacks _wt_engine_callbacks =
{
	.resume = ocf_engine_on_resume,
};

int ocf_write_wt(struct ocf_request *req)
{
	int lock = OCF_LOCK_NOT_ACQUIRED;


	/* Get OCF request - increase reference counter */
	ocf_req_get(req);

	/* Set resume handler */
	req->engine_handler = _ocf_write_wt_do;
	req->engine_cbs = &_wt_engine_callbacks;

	lock = ocf_engine_prepare_clines(req);

	if (!ocf_req_test_mapping_error(req)) {
		if (lock >= 0) {
			if (lock != OCF_LOCK_ACQUIRED) {
				/* WR lock was not acquired, need to wait for resume */
				OCF_DEBUG_RQ(req, "NO LOCK");
			} else {
				_ocf_write_wt_do(req);
			}
		} else {
			OCF_DEBUG_RQ(req, "LOCK ERROR %d\n", lock);
			req->complete(req, lock);
			ocf_req_put(req);
		}
	} else {
		ocf_req_clear(req);
		ocf_write_wi(req);
	}

	/* Put OCF request - decrease reference counter */
	ocf_req_put(req);

	return 0;
}
