/*
 * Copyright(c) 2012-2021 Intel Corporation
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef ENGINE_INV_H_
#define ENGINE_INV_H_

void ocf_engine_invalidate(struct ocf_request *req);

/* Like ocf_engine_invalidate(), but dirty sectors stay in the cache */
void ocf_engine_invalidate_keep_dirty(struct ocf_request *req);

#endif /* ENGINE_INV_H_ */
