/* SPDX-License-Identifier: GPL-3.0-or-later */

#include "retrofm_pdx.h"

#include <stdint.h>
#include <string.h>

/*
 * Streaming, bounds-checked implementation of the PDX and OKI ADPCM behavior
 * used by mdxtools commit 606e3a7009aa1a9dfa6bee8bc875dbd5483714e9.
 * No source file from the mdxtools checkout is vendored here.
 */

static const int16_t adpcm_step_sizes[49] = {
    16, 17, 19, 21, 23, 25, 28, 31, 34, 37,
    41, 45, 50, 55, 60, 66, 73, 80, 88, 97,
    107, 118, 130, 143, 157, 173, 190, 209, 230, 253,
    279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
    724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552
};

static const int8_t adpcm_step_adjustments[8] = {
    -1, -1, -1, -1, 2, 4, 6, 8
};

static uint32_t read_be_u32(const uint8_t *bytes) {
    return ((uint32_t)bytes[0] << 24U) |
           ((uint32_t)bytes[1] << 16U) |
           ((uint32_t)bytes[2] << 8U) |
           (uint32_t)bytes[3];
}

static bool valid_nonempty_range(size_t table_end,
                                 uint32_t offset,
                                 uint32_t length,
                                 size_t size) {
    size_t start;

    if ((uint64_t)offset < table_end || (uint64_t)offset > size) {
        return false;
    }
    start = (size_t)offset;
    return (uint64_t)length <= (uint64_t)(size - start);
}

/* PDX has no bank count. Only scan complete tables before the sample data. */
static bool scan_bank(const uint8_t *bytes, size_t size, size_t bank_end,
                      size_t *data_start) {
    const uint8_t *entry = bytes + bank_end - RETROFM_PDX_TABLE_BYTES;
    for (size_t index = 0U; index < RETROFM_PDX_SAMPLE_COUNT; ++index, entry += 8U) {
        uint32_t offset = read_be_u32(entry);
        uint32_t length = read_be_u32(entry + 4U);
        if (length == 0U) {
            continue;
        }
        if (!valid_nonempty_range(bank_end, offset, length, size)) {
            return false;
        }
        if (offset < *data_start) {
            *data_start = offset;
        }
    }
    return true;
}

retrofm_pdx_result retrofm_pdx_open(retrofm_pdx *pdx,
                                    const uint8_t *bytes,
                                    size_t size) {
    if (pdx == NULL) {
        return RETROFM_PDX_BAD_ARGUMENT;
    }
    memset(pdx, 0, sizeof(*pdx));
    if (bytes == NULL) {
        return RETROFM_PDX_BAD_ARGUMENT;
    }
    if (size < RETROFM_PDX_TABLE_BYTES) {
        return RETROFM_PDX_TRUNCATED;
    }

    size_t data_start = size;
    while (pdx->bank_count < RETROFM_PDX_BANK_LIMIT) {
        size_t bank_end = (pdx->bank_count + 1U) * RETROFM_PDX_TABLE_BYTES;
        if (bank_end > data_start) {
            break;
        }
        size_t next_data_start = data_start;
        if (!scan_bank(bytes, size, bank_end, &next_data_start)) {
            /* A trailing, non-table region can follow the available banks. */
            break;
        }
        data_start = next_data_start;
        ++pdx->bank_count;
    }
    if (pdx->bank_count == 0U) {
        return RETROFM_PDX_INVALID_RANGE;
    }

    pdx->bytes = bytes;
    pdx->size = size;
    return RETROFM_PDX_OK;
}

retrofm_pdx_result retrofm_pdx_get_sample(const retrofm_pdx *pdx,
                                          size_t sample_index,
                                          const uint8_t **bytes,
                                          size_t *length) {
    if (bytes != NULL) {
        *bytes = NULL;
    }
    if (length != NULL) {
        *length = 0U;
    }
    if (pdx == NULL || bytes == NULL || length == NULL || pdx->bytes == NULL) {
        return RETROFM_PDX_BAD_ARGUMENT;
    }
    if (sample_index >= RETROFM_PDX_BANK_LIMIT * RETROFM_PDX_SAMPLE_COUNT) {
        return RETROFM_PDX_INDEX_OUT_OF_RANGE;
    }

    if (sample_index >= pdx->bank_count * RETROFM_PDX_SAMPLE_COUNT) {
        return RETROFM_PDX_EMPTY_SAMPLE;
    }
    const uint8_t *entry = pdx->bytes + sample_index * 8U;
    uint32_t offset = read_be_u32(entry);
    uint32_t sample_size = read_be_u32(entry + 4U);
    if (sample_size == 0U) {
        return RETROFM_PDX_EMPTY_SAMPLE;
    }
    if (!valid_nonempty_range(pdx->bank_count * RETROFM_PDX_TABLE_BYTES,
                              offset, sample_size, pdx->size)) {
        return RETROFM_PDX_BAD_STATE;
    }

    *bytes = pdx->bytes + (size_t)offset;
    *length = (size_t)sample_size;
    return RETROFM_PDX_OK;
}

retrofm_pdx_result retrofm_adpcm_begin(retrofm_adpcm_decoder *decoder,
                                       const uint8_t *bytes,
                                       size_t length) {
    if (decoder == NULL) {
        return RETROFM_PDX_BAD_ARGUMENT;
    }
    memset(decoder, 0, sizeof(*decoder));
    if (length == 0U) {
        return RETROFM_PDX_EMPTY_SAMPLE;
    }
    if (bytes == NULL) {
        return RETROFM_PDX_BAD_ARGUMENT;
    }

    decoder->bytes = bytes;
    decoder->length = length;
    return RETROFM_PDX_OK;
}

retrofm_pdx_result retrofm_adpcm_next(retrofm_adpcm_decoder *decoder,
                                      int16_t *sample) {
    uint8_t code;
    int32_t step;
    int32_t delta;
    int32_t next_signal;
    int next_step_index;

    if (decoder == NULL || sample == NULL) {
        return RETROFM_PDX_BAD_ARGUMENT;
    }
    if (decoder->byte_position >= decoder->length) {
        return RETROFM_PDX_END;
    }
    if (decoder->bytes == NULL ||
        decoder->step_index >= sizeof(adpcm_step_sizes) /
                               sizeof(adpcm_step_sizes[0])) {
        return RETROFM_PDX_BAD_STATE;
    }

    if (decoder->high_nibble_next) {
        code = (uint8_t)(decoder->bytes[decoder->byte_position] >> 4U);
        decoder->high_nibble_next = false;
        ++decoder->byte_position;
    } else {
        code = (uint8_t)(decoder->bytes[decoder->byte_position] & 0x0FU);
        decoder->high_nibble_next = true;
    }

    step = adpcm_step_sizes[decoder->step_index];
    delta = step / 8;
    if ((code & 0x01U) != 0U) {
        delta += step / 4;
    }
    if ((code & 0x02U) != 0U) {
        delta += step / 2;
    }
    if ((code & 0x04U) != 0U) {
        delta += step;
    }
    if ((code & 0x08U) != 0U) {
        delta = -delta;
    }

    next_signal = (int32_t)decoder->signal + delta;
    if (next_signal > 2047) {
        next_signal = 2047;
    } else if (next_signal < -2048) {
        next_signal = -2048;
    }
    decoder->signal = (int16_t)next_signal;

    next_step_index = (int)decoder->step_index +
                      adpcm_step_adjustments[code & 0x07U];
    if (next_step_index < 0) {
        next_step_index = 0;
    } else if (next_step_index > 48) {
        next_step_index = 48;
    }
    decoder->step_index = (uint8_t)next_step_index;
    *sample = decoder->signal;
    return RETROFM_PDX_OK;
}

size_t retrofm_adpcm_samples_remaining(const retrofm_adpcm_decoder *decoder) {
    size_t remaining_bytes;
    size_t remaining_samples;

    if (decoder == NULL || decoder->byte_position >= decoder->length) {
        return 0U;
    }
    remaining_bytes = decoder->length - decoder->byte_position;
    if (remaining_bytes > SIZE_MAX / 2U) {
        return SIZE_MAX;
    }
    remaining_samples = remaining_bytes * 2U;
    if (decoder->high_nibble_next) {
        --remaining_samples;
    }
    return remaining_samples;
}

const char *retrofm_pdx_result_string(retrofm_pdx_result result) {
    switch (result) {
        case RETROFM_PDX_OK: return "ok";
        case RETROFM_PDX_END: return "end";
        case RETROFM_PDX_BAD_ARGUMENT: return "bad argument";
        case RETROFM_PDX_TRUNCATED: return "truncated PDX table";
        case RETROFM_PDX_INVALID_RANGE: return "invalid PDX sample range";
        case RETROFM_PDX_INDEX_OUT_OF_RANGE: return "PDX sample index out of range";
        case RETROFM_PDX_EMPTY_SAMPLE: return "empty PDX sample";
        case RETROFM_PDX_BAD_STATE: return "invalid decoder or PDX state";
        default: return "unknown PDX error";
    }
}
