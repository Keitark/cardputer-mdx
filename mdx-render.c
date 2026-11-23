/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Send FM-1 MDX playback and meter events to the CardputerZero player. */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <iconv.h>
#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>

#include "fm1_mdx.h"

enum {
    FRAMES = 1024,
    STEREO = 2,
    PLAYBACK_LOOPS = 2,
    LOOP_FADE_RATE = 16,
    DURATION_LIMIT_SECONDS = 20 * 60,
};

struct file_bytes {
    uint8_t *data;
    size_t size;
};

struct display_meters {
    fm1_note_motion spectrum[FM1_NOTE_BARS];
    fm1_part_motion parts[RETROFM_MDX_MAX_TRACKS];
};

static const uint8_t empty_bank[RETROFM_PDX_TABLE_BYTES] = {0};

static bool read_file(FILE *stream, struct file_bytes *bytes)
{
    if (fseek(stream, 0, SEEK_END) != 0) {
        return false;
    }
    long size = ftell(stream);
    if (size <= 0 || fseek(stream, 0, SEEK_SET) != 0) {
        return false;
    }
    uint8_t *data = malloc((size_t)size);
    if (!data) {
        return false;
    }
    if (fread(data, 1, (size_t)size, stream) != (size_t)size) {
        free(data);
        return false;
    }
    bytes->data = data;
    bytes->size = (size_t)size;
    return true;
}

static FILE *open_adjacent_bank(DIR *directory, const char *reference)
{
    const char *slash = strrchr(reference, '/');
    const char *name = slash ? slash + 1 : reference;
    const char *backslash = strrchr(name, '\\');
    if (backslash) {
        name = backslash + 1;
    }
    size_t length = strlen(name);
    const char *suffix =
        length >= 4 && strcasecmp(name + length - 4, ".pdx") == 0 ? "" : ".pdx";
    char filename[NAME_MAX + 1];
    int count = snprintf(filename, sizeof filename, "%s%s", name, suffix);
    if (!length || count < 0 || (size_t)count >= sizeof filename) {
        return NULL;
    }

    int directory_fd = dirfd(directory);
    int fd = openat(directory_fd, filename, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) {
        struct dirent *entry;
        rewinddir(directory);
        while ((entry = readdir(directory))) {
            if (strcasecmp(entry->d_name, filename) == 0) {
                fd = openat(directory_fd, entry->d_name, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
                if (fd >= 0) {
                    break;
                }
            }
        }
    }
    if (fd < 0) {
        return NULL;
    }

    FILE *stream = fdopen(fd, "rb");
    if (!stream) {
        close(fd);
    }
    return stream;
}

static FILE *open_pdx(const char *mdx_path, const retrofm_mdx_span *name)
{
    if (!name->size) {
        return NULL;
    }
    const char *slash = strrchr(mdx_path, '/');
    char *path = slash ? strndup(mdx_path, (size_t)(slash - mdx_path + 1)) : strdup(".");
    if (!path) {
        return NULL;
    }
    DIR *directory = opendir(path);
    free(path);
    if (!directory) {
        return NULL;
    }

    /* Decode before splitting DOS paths since 0x5c can be a CP932 trail byte. */
    iconv_t conversion = iconv_open("UTF-8", "CP932");
    FILE *stream = NULL;
    if (conversion != (iconv_t)-1) {
        char decoded[PATH_MAX];
        char *input = (char *)name->data;
        char *output = decoded;
        size_t in_left = name->size;
        size_t out_left = sizeof decoded - 1;
        if (iconv(conversion, &input, &in_left, &output, &out_left) != (size_t)-1) {
            *output = '\0';
            stream = open_adjacent_bank(directory, decoded);
        }
        iconv_close(conversion);
    }
    if (!stream) {
        /* Some Linux collections store UTF-8 names directly in MDX headers. */
        stream = open_adjacent_bank(directory, (const char *)name->data);
    }
    closedir(directory);
    return stream;
}

static bool read_track(const char *path, struct file_bytes *mdx)
{
    FILE *stream = fopen(path, "rb");
    if (!stream) {
        fprintf(stderr, "ERROR Could not open MDX track\n");
        return false;
    }
    bool loaded = read_file(stream, mdx);
    fclose(stream);
    if (!loaded) {
        fprintf(stderr, "ERROR Could not read MDX track\n");
    }
    return loaded;
}

static retrofm_mdx_result load_song(fm1_mdx_player *player, const char *path,
                                    const struct file_bytes *mdx, struct file_bytes *pdx,
                                    bool *has_pdx)
{
    retrofm_mdx header;
    retrofm_mdx_result result = retrofm_mdx_open(&header, mdx->data, mdx->size);
    if (result != RETROFM_MDX_OK) {
        return result;
    }
    if (header.uses_pdx) {
        FILE *stream = open_pdx(path, &header.pdx_name);
        if (stream) {
            (void)read_file(stream, pdx);
            fclose(stream);
        }
    }

    result = (retrofm_mdx_result)fm1_mdx_load(player, mdx->data, mdx->size, pdx->data, pdx->size);
    *has_pdx = header.uses_pdx && result == RETROFM_MDX_OK;
    if (result == RETROFM_MDX_MISSING_PDX) {
        /* Keep the PCM parts on the sequencer's timeline when the bank is unavailable. */
        result = (retrofm_mdx_result)fm1_mdx_load(player, mdx->data, mdx->size,
                                                  empty_bank, sizeof empty_bank);
    }
    return result;
}

static retrofm_mdx_result reload_song(fm1_mdx_player *player, const struct file_bytes *mdx,
                                      const struct file_bytes *pdx, bool has_pdx)
{
    return (retrofm_mdx_result)fm1_mdx_load(player, mdx->data, mdx->size,
                                           has_pdx ? pdx->data : empty_bank,
                                           has_pdx ? pdx->size : sizeof empty_bank);
}

static void print_title(const retrofm_mdx_span *title)
{
    fputs("TITLE ", stderr);
    for (size_t i = 0; i < title->size && i < 1024; ++i) {
        uint8_t character = title->data[i];
        fputc(character == '\r' || character == '\n' || !character ? ' ' : character, stderr);
    }
    fputc('\n', stderr);
}

static void advance_meters(fm1_mdx_player *player, struct display_meters *meters,
                           uint64_t elapsed_ms, uint32_t step_ms)
{
    uint8_t volume[RETROFM_MDX_MAX_TRACKS];
    uint8_t onset[RETROFM_MDX_MAX_TRACKS];
    uint16_t held;
    uint16_t triggered = fm1_mdx_part_activity(player, volume, onset, &held);

    fprintf(stderr, "METER %" PRIu64, elapsed_ms);
    for (size_t i = 0; i < FM1_NOTE_BARS; ++i) {
        fm1_note_step(&meters->spectrum[i], player->meters.note_energy[i], step_ms);
        player->meters.note_energy[i] = 0;
        fprintf(stderr, " %u %u", meters->spectrum[i].step, meters->spectrum[i].peak);
    }
    for (size_t i = 0; i < RETROFM_MDX_MAX_TRACKS; ++i) {
        unsigned int bit = 1u << i;
        fm1_part_step(&meters->parts[i], triggered & bit, onset[i], volume[i], held & bit, step_ms);
        fprintf(stderr, " %u %u", meters->parts[i].step, (volume[i] * 28u + 254u) / 255u);
    }
    fputc('\n', stderr);
}

static retrofm_mdx_result fade_after_loops(retrofm_mdx_sequencer *sequence)
{
    if (sequence->ended || sequence->fade_rate != 0) {
        return RETROFM_MDX_OK;
    }
    for (size_t i = 0; i < sequence->mdx->track_count; ++i) {
        const retrofm_mdx_track_state *track = &sequence->tracks[i];
        if (track->used && !track->ended && track->loop_count < PLAYBACK_LOOPS) {
            return RETROFM_MDX_OK;
        }
    }
    return retrofm_mdx_sequencer_start_fade(sequence, LOOP_FADE_RATE);
}

/* First DAC sample at or after the timer boundary. */
static uint64_t sample_at_cycle(uint64_t cycles)
{
    return cycles / RETROFM_PL_CLOCK_HZ * FM1_MDX_RATE +
        (cycles % RETROFM_PL_CLOCK_HZ * FM1_MDX_RATE + RETROFM_PL_CLOCK_HZ - 1) /
        RETROFM_PL_CLOCK_HZ;
}

/* Scan the sequence before writing PCM to detect unsupported commands early. */
static retrofm_mdx_result scan_song(fm1_mdx_player *player, uint64_t *duration_ms)
{
    retrofm_mdx_sequencer *sequence = &player->sequence;
    const uint64_t limit = (uint64_t)DURATION_LIMIT_SECONDS * RETROFM_PL_CLOCK_HZ;
    while (!sequence->ended && sequence->current_cycles < limit) {
        retrofm_mdx_result result = retrofm_mdx_sequencer_tick(sequence);
        if (result == RETROFM_MDX_OK) {
            result = fade_after_loops(sequence);
        }
        if (result != RETROFM_MDX_OK) {
            return result;
        }
    }
    if (sequence->ended) {
        uint64_t blocks = sample_at_cycle(sequence->current_cycles) / FRAMES + 1;
        *duration_ms = blocks * FRAMES * 1000u / FM1_MDX_RATE;
    }
    return RETROFM_MDX_OK;
}

static void update_mix_mode(int *fd, bool *mono)
{
    char commands[64];
    while (*fd >= 0) {
        ssize_t count = read(*fd, commands, sizeof commands);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            if (count == 0 || (errno != EAGAIN && errno != EWOULDBLOCK)) {
                close(*fd);
                *fd = -1;
            }
            break;
        }
        for (ssize_t i = 0; i < count; ++i) {
            if (commands[i] == 'M' || commands[i] == 'S') {
                *mono = commands[i] == 'M';
            }
        }
    }
}

static retrofm_mdx_result render_block(fm1_mdx_player *player, int16_t *samples, uint64_t written)
{
    retrofm_mdx_sequencer *sequence = &player->sequence;
    for (size_t offset = 0; offset < FRAMES;) {
        size_t count = FRAMES - offset;
        if (count > FM1_MDX_BLOCK) {
            count = FM1_MDX_BLOCK;
        }
        if (player->playing) {
            uint64_t tick = sample_at_cycle(sequence->current_cycles + sequence->cycles_per_tick);
            uint64_t through_tick = tick > written + offset ? tick - written - offset + 1 : 1;
            if (count > through_tick) {
                count = (size_t)through_tick;
            }
        }
        retrofm_mdx_result result =
            (retrofm_mdx_result)fm1_mdx_render(player, samples + offset * STEREO, count);
        if (result == RETROFM_MDX_OK) {
            result = fade_after_loops(sequence);
        }
        if (result != RETROFM_MDX_OK) {
            return result;
        }
        offset += count;
    }
    return RETROFM_MDX_OK;
}

static int render(fm1_mdx_player *player, int mix_fd)
{
    /* The meter clock follows the PCM stream and stays close to audible playback. */
    (void)fcntl(STDOUT_FILENO, F_SETPIPE_SZ, 4096);
    struct display_meters meters = {0};
    int16_t samples[FRAMES * STEREO];
    uint64_t written = 0;
    uint64_t elapsed_ms = 0;
    bool mono = false;
    int status = 0;
    do {
        retrofm_mdx_result result = render_block(player, samples, written);
        if (result != RETROFM_MDX_OK) {
            fprintf(stderr, "ERROR %s\n", retrofm_mdx_result_string(result));
            status = 1;
            break;
        }
        update_mix_mode(&mix_fd, &mono);
        if (mono) {
            for (size_t i = 0; i < FRAMES * STEREO; i += STEREO) {
                int16_t mixed = ((int32_t)samples[i] + samples[i + 1]) / 2;
                samples[i] = samples[i + 1] = mixed;
            }
        }
        if (fwrite(samples, sizeof samples[0] * STEREO, FRAMES, stdout) != FRAMES) {
            status = 1;
            break;
        }
        written += FRAMES;
        uint64_t audio_ms = written * 1000u / FM1_MDX_RATE;
        advance_meters(player, &meters, audio_ms, (uint32_t)(audio_ms - elapsed_ms));
        elapsed_ms = audio_ms;
    } while (player->playing);
    if (mix_fd >= 0) {
        close(mix_fd);
    }
    return status;
}

int main(int argc, char **argv)
{
    bool duration_only = argc == 3 && !strcmp(argv[1], "--duration");
    int mix_fd = -1;
    if (argc == 4 && !strcmp(argv[1], "--mix-fd")) {
        char *end;
        errno = 0;
        long fd = strtol(argv[2], &end, 10);
        if (errno || *end || fd < 3 || fd > INT_MAX) {
            fprintf(stderr, "ERROR Invalid mix control descriptor\n");
            return 2;
        }
        mix_fd = (int)fd;
        int flags = fcntl(mix_fd, F_GETFL);
        if (flags < 0 || (flags & O_ACCMODE) == O_WRONLY ||
            fcntl(mix_fd, F_SETFL, flags | O_NONBLOCK) < 0) {
            fprintf(stderr, "ERROR Could not open mix control descriptor\n");
            return 2;
        }
    } else if (argc != 2 && !duration_only) {
        fprintf(stderr, "Usage: mdx-render [--duration | --mix-fd N] FILE.mdx\n");
        return 2;
    }

    struct file_bytes mdx = {0};
    if (!read_track(argv[argc - 1], &mdx)) {
        return 1;
    }

    fm1_mdx_player player;
    struct file_bytes pdx = {0};
    bool has_pdx = false;
    uint64_t duration_ms = 0;
    retrofm_mdx_result result = load_song(&player, argv[argc - 1], &mdx, &pdx, &has_pdx);
    if (result == RETROFM_MDX_OK) {
        result = scan_song(&player, &duration_ms);
    }
    if (result == RETROFM_MDX_OK && !duration_only) {
        result = reload_song(&player, &mdx, &pdx, has_pdx);
    }

    int status = 1;
    if (result == RETROFM_MDX_OK) {
        fprintf(stderr, "DECODER RETROFM\n");
        if (duration_only) {
            if (duration_ms) {
                printf("%" PRIu64 "\n", duration_ms);
                status = 0;
            }
        } else {
            print_title(&player.mdx.title);
            fprintf(stderr, "ENGINE %s\n", has_pdx ? "FM + PCM" : "FM");
            status = render(&player, mix_fd);
        }
    }
    free(pdx.data);
    free(mdx.data);
    if (result != RETROFM_MDX_OK) {
        fprintf(stderr, "ERROR %s\n", retrofm_mdx_result_string(result));
    }
    return status;
}
