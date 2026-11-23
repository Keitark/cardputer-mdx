/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Connect the mdxmini audio driver to the FM-1 MDX display envelopes. */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>

#include "mdxmini.h"
#include "fm1_meters.h"

enum { RATE = 44100, FRAMES = 1024, STEREO = 2 };

static MDX_DATA *meter_song;
static fm1_meters meters;
static fm1_note_motion spectrum[FM1_NOTE_BARS];
static fm1_part_motion parts[MDX_MAX_TRACK_NUMBER];
static unsigned char key_code[8];
static unsigned char onset[MDX_MAX_TRACK_NUMBER];
static unsigned int held, seen, triggered;

static unsigned int velocity(unsigned int part)
{
    int volume = meter_song->track[part].volume;
    return volume < 0 ? 0 : volume > 127 ? 127 : (unsigned int)volume;
}

static unsigned char configured_level(unsigned int level)
{
    unsigned int step = level >> 2;
    return step > 4 ? (step - 4) * 255 / 28 : 0;
}

static void record_onset(unsigned int part, unsigned int midi)
{
    unsigned int level = velocity(part);
    unsigned int bit = 1u << part;
    seen |= bit;
    held |= bit;
    triggered |= bit;
    onset[part] = configured_level(level);
    fm1_meters_note(&meters, midi, level);
}

void __real_YM2151WriteReg(void *chip, int reg, int value);
void __wrap_YM2151WriteReg(void *chip, int reg, int value)
{
    __real_YM2151WriteReg(chip, reg, value);
    if (!meter_song) {
        return;
    }
    if (reg >= 0x28 && reg <= 0x2f) {
        key_code[reg - 0x28] = value & 0x7f;
    } else if (reg == 8) {
        static const unsigned char semitone[] = {0,1,2,2,3,4,5,5,6,7,8,8,9,10,11,11};
        unsigned int part = value & 7;
        if (value & 0x78) {
            unsigned int pitch = key_code[part];
            record_onset(part, 13 + (pitch >> 4) * 12 + semitone[pitch & 15]);
        } else {
            held &= ~(1u << part);
        }
    }
}

int __real_pcm8_note_on(int, int *, int, int *, int, songdata *);
int __wrap_pcm8_note_on(int channel, int *pcm, int count, int *original, int size, songdata *data)
{
    int result = __real_pcm8_note_on(channel, pcm, count, original, size, data);
    if (!result && meter_song && channel >= 0 && channel < 8 && (count > 0 || size > 0)) {
        unsigned int part = channel + 8;
        if (!(held & (1u << part))) {
            int note = meter_song->track[part].note;
            record_onset(part, note >= 0 ? note + 13 : 13);
        }
    }
    return result;
}

int __real_pcm8_note_off(int, songdata *);
int __wrap_pcm8_note_off(int channel, songdata *data)
{
    if (channel >= 0 && channel < 8) {
        held &= ~(1u << (channel + 8));
    }
    return __real_pcm8_note_off(channel, data);
}

static void advance_meters(unsigned int elapsed_ms, unsigned int step_ms, int tracks)
{
    unsigned char volume[MDX_MAX_TRACK_NUMBER] = {0};
    if (tracks > MDX_MAX_TRACK_NUMBER) {
        tracks = MDX_MAX_TRACK_NUMBER;
    }
    for (unsigned int i = 0; i < FM1_NOTE_BARS; ++i) {
        fm1_note_step(&spectrum[i], meters.note_energy[i], step_ms);
        meters.note_energy[i] = 0;
    }
    for (int i = 0; i < tracks; ++i) {
        unsigned int bit = 1u << i;
        volume[i] = seen & bit ? configured_level(velocity(i)) : 0;
        fm1_part_step(&parts[i], triggered & bit, onset[i], volume[i], held & bit, step_ms);
    }
    triggered = 0;

    fprintf(stderr, "METER %u", elapsed_ms);
    for (unsigned int i = 0; i < FM1_NOTE_BARS; ++i) {
        fprintf(stderr, " %u %u", spectrum[i].step, spectrum[i].peak);
    }
    for (int i = 0; i < MDX_MAX_TRACK_NUMBER; ++i) {
        fprintf(stderr, " %u %u", parts[i].step, (volume[i] * 28u + 254u) / 255u);
    }
    fputc('\n', stderr);
}

FILE *__real_fopen(const char *path, const char *mode);

/* X68000 PDX references are case-insensitive. Linux filenames can be mixed case. */
FILE *__wrap_fopen(const char *path, const char *mode)
{
    FILE *file = __real_fopen(path, mode);
    const char *extension = strrchr(path, '.');
    if (file || !extension || strcasecmp(extension, ".pdx") || strcmp(mode, "rb")) {
        return file;
    }

    const char *slash = strrchr(path, '/');
    const char *name = slash ? slash + 1 : path;
    char directory[PATH_MAX] = ".";
    if (slash) {
        size_t length = slash - path + 1;
        if (length >= sizeof directory) {
            return NULL;
        }
        memcpy(directory, path, length);
        directory[length] = '\0';
    }
    DIR *entries = opendir(directory);
    if (!entries) {
        return NULL;
    }
    struct dirent *entry;
    while ((entry = readdir(entries)) != NULL) {
        if (!strcasecmp(entry->d_name, name)) {
            char candidate[PATH_MAX];
            int length = snprintf(candidate, sizeof candidate, "%s/%s", directory, entry->d_name);
            if (length >= 0 && (size_t)length < sizeof candidate) {
                file = __real_fopen(candidate, mode);
            }
            break;
        }
    }
    closedir(entries);
    return file;
}

/* Count the same padded blocks as playback without rendering the PCM. */
static int print_duration(t_mdxmini *song)
{
    const uint64_t limit = 20u * 60u * RATE / FRAMES;
    uint64_t blocks = 0;
    int more;
    do {
        more = mdx_calc_log(song, NULL, FRAMES);
        ++blocks;
    } while (more && blocks < limit);
    if (more) {
        return 1;
    }
    printf("%llu\n", (unsigned long long)(blocks * FRAMES * 1000u / RATE));
    return 0;
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

int main(int argc, char **argv)
{
    int duration_only = argc == 3 && !strcmp(argv[1], "--duration");
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

    t_mdxmini song = {0};
    mdx_set_rate(RATE);
    if (mdx_open(&song, argv[argc - 1], NULL) != 0) {
        fprintf(stderr, "ERROR Could not open MDX track\n");
        return 1;
    }
    if (song.channels != STEREO) {
        fprintf(stderr, "ERROR Unsupported audio channel count\n");
        mdx_close(&song);
        return 1;
    }

    mdx_set_max_loop(&song, 2);
    if (duration_only) {
        int result = print_duration(&song);
        mdx_close(&song);
        return result;
    }
    char title[MDX_MAX_TITLE_LENGTH + 1] = {0};
    mdx_get_title(&song, title);
    title[MDX_MAX_TITLE_LENGTH] = '\0';
    for (char *character = title; *character; ++character) {
        if (*character == '\r' || *character == '\n') {
            *character = ' ';
        }
    }
    fprintf(stderr, "TITLE %s\n", title);
    fprintf(stderr, "ENGINE %s\n", song.pdx ? "FM + PCM" : "FM");

    meter_song = song.mdx;
    /* Keep the display close to the audio clock when stdout is a pipe. */
    (void)fcntl(STDOUT_FILENO, F_SETPIPE_SZ, 4096);
    const int tracks = mdx_get_tracks(&song);
    short samples[FRAMES * STEREO];
    uint64_t written_frames = 0;
    unsigned int elapsed_ms = 0;
    bool mono = false;
    int more;
    do {
        more = mdx_calc_sample(&song, samples, FRAMES);
        update_mix_mode(&mix_fd, &mono);
        if (mono) {
            for (int i = 0; i < FRAMES * STEREO; i += STEREO) {
                short mixed = ((int32_t)samples[i] + samples[i + 1]) / 2;
                samples[i] = samples[i + 1] = mixed;
            }
        }
        if (fwrite(samples, sizeof(short) * STEREO, FRAMES, stdout) != FRAMES) {
            break;
        }
        written_frames += FRAMES;
        unsigned int audio_ms = written_frames * 1000u / RATE;
        advance_meters(audio_ms, audio_ms - elapsed_ms, tracks);
        elapsed_ms = audio_ms;
    } while (more);
    meter_song = NULL;
    mdx_close(&song);
    return 0;
}
