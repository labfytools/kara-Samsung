/* Isolated graph fixture. The three optional paths share the driver cursor. */
#define _GNU_SOURCE
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <pipewire/filter.h>
#include <pipewire/pipewire.h>

struct fixture {
    struct pw_main_loop *loop;
    void *ports[3];
    float *pcm;
    unsigned frames, cursor, seen;
    _Atomic unsigned cue;
    bool inject_gap;
};

static void process(void *arg, struct spa_io_position *position)
{
    struct fixture *fixture = arg;
    unsigned n = (unsigned)position->clock.duration;
    unsigned cue = atomic_load_explicit(&fixture->cue, memory_order_relaxed);
    if (fixture->seen != cue) {
        fixture->seen = cue;
        fixture->cursor = 0;
    }
    for (unsigned channel = 0; channel < 3; channel++) {
        struct pw_buffer *pw = pw_filter_dequeue_buffer(fixture->ports[channel]);
        if (!pw)
            continue;
        struct spa_data *plane = pw->buffer->datas;
        float *target = plane->data;
        if (!target || !plane->chunk || plane->maxsize / sizeof(float) < n) {
            pw_filter_queue_buffer(fixture->ports[channel], pw);
            continue;
        }
        memset(target, 0, n * sizeof(float));
        if (fixture->cursor < fixture->frames) {
            unsigned remaining = fixture->frames - fixture->cursor;
            unsigned available = remaining < n ? remaining : n;
            const float *from = fixture->pcm + channel * fixture->frames + fixture->cursor;
            memcpy(target, from, available * sizeof(float));
        }
        plane->chunk->offset = 0;
        plane->chunk->size = n * sizeof(float);
        plane->chunk->stride = sizeof(float);
        plane->chunk->flags = 0;
        if (channel == 0 && fixture->inject_gap && fixture->cursor < 36000 && fixture->cursor + n >= 36000)
            plane->chunk->size -= sizeof(float);
        pw_filter_queue_buffer(fixture->ports[channel], pw);
    }
    if (fixture->cursor < fixture->frames)
        fixture->cursor += n;
}

static void signal_event(void *arg, int sig)
{
    struct fixture *fixture = arg;
    if (sig == SIGUSR1)
        atomic_fetch_add_explicit(&fixture->cue, 1, memory_order_relaxed);
    else
        pw_main_loop_quit(fixture->loop);
}

int main(int argc, char **argv)
{
    if (argc != 4)
        return 2;
    struct fixture fixture = { .frames = (unsigned)strtoul(argv[2], NULL, 10),
                               .inject_gap = strcmp(argv[3], "gap") == 0 };
    if (fixture.frames < 1 || fixture.frames > 96000)
        return 2;
    fixture.cursor = fixture.frames;
    fixture.pcm = malloc((size_t)fixture.frames * 3 * sizeof(float));
    if (!fixture.pcm || fread(fixture.pcm, sizeof(float), fixture.frames * 3, stdin) != fixture.frames * 3)
        return 2;
    pw_init(NULL, NULL);
    fixture.loop = pw_main_loop_new(NULL);
    struct pw_loop *loop = pw_main_loop_get_loop(fixture.loop);
    pw_loop_add_signal(loop, SIGUSR1, signal_event, &fixture);
    pw_loop_add_signal(loop, SIGTERM, signal_event, &fixture);
    const struct pw_filter_events events = { PW_VERSION_FILTER_EVENTS, .process = process };
    struct pw_properties *props = pw_properties_new(PW_KEY_NODE_NAME, argv[1],
        PW_KEY_MEDIA_TYPE, "Audio", PW_KEY_NODE_AUTOCONNECT, "false", PW_KEY_NODE_WANT_DRIVER, "true",
        PW_KEY_NODE_ASYNC, strcmp(argv[3], "async") == 0 ? "true" : "false", NULL);
    /* The dedicated high-priority driver pulls the already-running sink into
     * the mic's subgraph when the witness joins the two sets of ports. */
    if (strcmp(argv[3], "driver-group") == 0)
        pw_properties_set(props, PW_KEY_NODE_GROUP, "kara-v51-mic-group");
    struct pw_filter *filter = pw_filter_new_simple(loop, "V5 graph fixture", props, &events, &fixture);
    const char *names[] = { "direct", "playback-left", "playback-right" };
    const char *channels[] = { "MONO", "FL", "FR" };
    for (unsigned i = 0; i < 3; i++) {
        fixture.ports[i] = pw_filter_add_port(filter, PW_DIRECTION_OUTPUT, PW_FILTER_PORT_FLAG_MAP_BUFFERS, 1,
            pw_properties_new(PW_KEY_PORT_NAME, names[i], PW_KEY_AUDIO_CHANNEL, channels[i],
                              PW_KEY_FORMAT_DSP, "32 bit float mono audio", NULL), NULL, 0);
    }
    enum pw_filter_flags flags = PW_FILTER_FLAG_RT_PROCESS;
    if (strcmp(argv[3], "async") == 0)
        flags |= PW_FILTER_FLAG_ASYNC;
    if (pw_filter_connect(filter, flags, NULL, 0) < 0)
        return 1;
    pw_main_loop_run(fixture.loop);
    pw_filter_destroy(filter);
    pw_main_loop_destroy(fixture.loop);
    pw_deinit();
    free(fixture.pcm);
    return 0;
}
