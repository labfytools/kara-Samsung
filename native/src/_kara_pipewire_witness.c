/* V5.1 DSP taps. The two single-endpoint modes never join capture and playback graphs. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <float.h>
#include <limits.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <pipewire/filter.h>
#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>

enum { CHANNELS = 3, HEADER = 48, MAX_FRAMES = 8192, RING = 64, MAX_NODES = 2048, MAX_PLAYBACK_LINKS = 128 };
enum { ROUTE_NONE, ROUTE_KARA, ROUTE_MIXED, ROUTE_UNKNOWN };
enum { ROUTE_MASK = 3, PREFLIGHT_STEREO_ACTIVE = 4 };
enum { FAULT_NONE, FAULT_CLOCK, FAULT_BUFFER, FAULT_OVERFLOW };
enum tap_result { TAP_INVALID, TAP_IDLE, TAP_PRESENT };

_Static_assert(CHAR_BIT == 8 && sizeof(float) == 4 && FLT_RADIX == 2 && FLT_MANT_DIG == 24,
               "The diagnostic wire format requires binary32.");
_Static_assert(ATOMIC_INT_LOCK_FREE == 2, "Realtime handoff requires lock-free atomics.");

struct packet {
    size_t size;
    unsigned char wire[HEADER + CHANNELS * MAX_FRAMES * 4];
};

struct data;
struct tap {
    struct data *owner;
    const char *name, *channel;
    uint32_t peer, own, link_id;
    void *port;
    struct pw_link *link;
    struct spa_hook listener;
    bool active, format_known;
};

struct graph_node {
    uint32_t id;
    bool player, preflight;
};

struct playback_link {
    struct data *owner;
    struct pw_link *proxy;
    struct spa_hook listener;
    uint32_t id, upstream;
    bool active, known;
};

struct endpoint {
    struct data *owner;
    struct pw_node *proxy;
    struct spa_hook listener;
    uint32_t id, driver;
    bool running, can_drive;
};

struct data {
    struct pw_core *core;
    struct pw_registry *registry;
    struct pw_filter *filter;
    const char *mic_name, *sink_name, *preflight_player, *problem;
    char filter_name[64];
    uint32_t mic, sink, own;
    struct tap tap[CHANNELS];
    struct endpoint endpoint[2];
    struct graph_node graph[MAX_NODES];
    size_t graph_size;
    struct playback_link playback[MAX_PLAYBACK_LINKS];
    int completed_sync;
    enum pw_filter_state state;
    bool armed;
    unsigned enabled_taps;
    bool started;                  /* Only the data thread reads or writes this. */
    _Atomic unsigned ready, expected_driver, fault, produced, consumed, route;
    struct packet ring[RING];
};

static volatile sig_atomic_t stop;

static void stop_signal(int signal_number)
{
    (void)signal_number;
    stop = 1;
}

static void put32(unsigned char *dst, uint32_t value)
{
    for (unsigned i = 0; i < 4; i++)
        dst[i] = (unsigned char)(value >> (8 * i));
}

static void put64(unsigned char *dst, uint64_t value)
{
    put32(dst, (uint32_t)value);
    put32(dst + 4, (uint32_t)(value >> 32));
}

static void copy_f32(unsigned char *dst, const void *src, uint32_t count)
{
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
    memcpy(dst, src, (size_t)count * 4);
#else
    for (uint32_t i = 0; i < count; i++) {
        uint32_t bits;
        memcpy(&bits, (const unsigned char *)src + i * 4, 4);
        put32(dst + i * 4, bits);
    }
#endif
}

static enum tap_result copy_tap(struct tap *tap, unsigned char *dst, uint32_t frames, bool monitor,
                                bool *has_header)
{
    struct pw_buffer *pw = pw_filter_dequeue_buffer(tap->port);
    if (!pw)
        return TAP_IDLE;
    struct spa_buffer *buffer = pw->buffer;
    enum tap_result result = dst ? TAP_INVALID : TAP_IDLE;
    if (dst && buffer && buffer->n_datas == 1) {
        struct spa_data *plane = buffer->datas;
        const struct spa_chunk *chunk = plane->chunk;
        struct spa_meta_header *meta = spa_buffer_find_meta_data(buffer, SPA_META_Header, sizeof(*meta));
        *has_header = meta != NULL;
        uint32_t bytes = frames * 4;
        bool valid = chunk && plane->data && chunk->offset == 0 && chunk->size == bytes
            && bytes <= plane->maxsize && chunk->stride == 4
            && !(chunk->flags & SPA_CHUNK_FLAG_CORRUPTED)
            && (monitor || !(chunk->flags & SPA_CHUNK_FLAG_EMPTY))
            && (!meta || (!meta->offset && !(meta->flags & (SPA_META_HEADER_FLAG_CORRUPTED |
                          SPA_META_HEADER_FLAG_DISCONT | SPA_META_HEADER_FLAG_GAP))));
        if (valid) {
            result = TAP_PRESENT;
            if (chunk->flags & SPA_CHUNK_FLAG_EMPTY)
                memset(dst, 0, bytes);
            else
                copy_f32(dst, plane->data, frames);
        }
    }
    if (pw_filter_queue_buffer(tap->port, pw) < 0)
        return TAP_INVALID;
    return result;
}

static void process(void *userdata, struct spa_io_position *position)
{
    struct data *data = userdata;
    const struct spa_io_clock *clock = position ? &position->clock : NULL;
    bool ready = atomic_load_explicit(&data->ready, memory_order_acquire)
        && !atomic_load_explicit(&data->fault, memory_order_relaxed);
    bool clock_ok = clock && clock->duration > 0 && clock->duration <= MAX_FRAMES
        && clock->rate.num == 1 && clock->rate.denom == 48000
        && clock->id == atomic_load_explicit(&data->expected_driver, memory_order_relaxed)
        && clock->nsec != 0
        && !(clock->flags & (SPA_IO_CLOCK_FLAG_XRUN_RECOVER | SPA_IO_CLOCK_FLAG_DISCONT));
    uint32_t count = clock_ok ? (uint32_t)clock->duration : 0;
    unsigned head = atomic_load_explicit(&data->produced, memory_order_relaxed);
    unsigned tail = atomic_load_explicit(&data->consumed, memory_order_acquire);
    bool space = head - tail < RING;
    struct packet *slot = ready && clock_ok && space ? &data->ring[head % RING] : NULL;
    uint32_t meta_mask = 0, present_mask = 0;
    unsigned playback = atomic_load_explicit(&data->route, memory_order_acquire);
    unsigned route = playback & ROUTE_MASK;
    /* Preflight waits for its own stereo links and requires actual monitor
     * buffers. Other active applications cannot satisfy that startup check. */
    bool valid = slot && (!data->preflight_player ||
                          ((playback & PREFLIGHT_STEREO_ACTIVE) && route == ROUTE_MIXED));
    for (unsigned i = 0; i < CHANNELS; i++) {
        bool has_header = false;
        unsigned char *dest = slot ? slot->wire + HEADER + i * count * 4 : NULL;
        if (!(data->enabled_taps & (1u << i))) {
            if (dest)
                memset(dest, 0, count * 4);
            continue;
        }
        bool idle_monitor = i != 0 && route == ROUTE_NONE;
        enum tap_result result = copy_tap(&data->tap[i], idle_monitor ? NULL : dest,
                                         count, i != 0, &has_header);
        if (result == TAP_PRESENT) {
            present_mask |= 1u << i;
        } else if (result == TAP_IDLE && idle_monitor && dest) {
            /* Idle adapters may send no buffers or capacity-sized
             * placeholders. Report no monitor PCM when there is no active
             * playback link, regardless of what is in those buffers. */
            memset(dest, 0, count * 4);
        } else {
            valid = false;
        }
        if (has_header && result == TAP_PRESENT)
            meta_mask |= 1u << i;
    }
    if (!ready)
        return;
    if (!clock_ok || !space || (!valid && data->started)) {
        atomic_store_explicit(&data->fault, !clock_ok ? FAULT_CLOCK :
                              !space ? FAULT_OVERFLOW : FAULT_BUFFER, memory_order_release);
        return;
    }
    if (!valid)
        return;                    /* Links can acquire their first cycle at different times. */
    data->started = true;
    /* KPW1, fixed-width LE header, followed by three planar F32LE channels.
     * The flags carry a SPA header mask in bits 0..2 and actual PCM presence
     * in bits 8..10. Placeholder planes must never become monitor evidence.
     * position+i names the logical frame at each synchronous tap. Nothing
     * in this packet timestamps the ADC, DAC, or userspace pipe delivery. */
    memcpy(slot->wire, "KPW1", 4);
    put32(slot->wire + 4, count);
    put32(slot->wire + 8, clock->rate.denom);
    put32(slot->wire + 12, clock->id);
    put32(slot->wire + 16, route);
    put32(slot->wire + 20, meta_mask | (present_mask << 8));
    put64(slot->wire + 24, clock->position);
    put64(slot->wire + 32, clock->nsec);
    put64(slot->wire + 40, clock->xrun);
    slot->size = HEADER + (size_t)CHANNELS * count * 4;
    /* Publish a complete slot to the main thread, which alone writes stdout. */
    atomic_store_explicit(&data->produced, head + 1, memory_order_release);
}

static bool equals(const char *left, const char *right)
{
    return left && right && strcmp(left, right) == 0;
}

static uint32_t id_value(const struct spa_dict *props, const char *key)
{
    uint32_t id;
    const char *value = spa_dict_lookup(props, key);
    return value && spa_atou32(value, &id, 10) ? id : SPA_ID_INVALID;
}

static bool kara_node(const char *name)
{
    const char prefix[] = "kara-microphone-";
    if (!name || strncmp(name, prefix, sizeof(prefix) - 1))
        return false;
    const char *suffix = name + sizeof(prefix) - 1;
    return strlen(suffix) == 16 && strspn(suffix, "0123456789abcdef") == 16;
}

static void update_route(struct data *data)
{
    if (data->sink == SPA_ID_INVALID)
        return;
    unsigned route = ROUTE_NONE;
    uint32_t player = SPA_ID_INVALID;
    unsigned preflight_links = 0;
    for (size_t i = 0; i < MAX_PLAYBACK_LINKS; i++) {
        const struct playback_link *link = &data->playback[i];
        if (!link->proxy)
            continue;
        if (!link->known) {
            if (route < ROUTE_MIXED)
                route = ROUTE_UNKNOWN;
            continue;
        }
        if (!link->active)
            continue;
        bool is_kara = false;
        for (size_t j = 0; j < data->graph_size; j++) {
            if (data->graph[j].id == link->upstream) {
                is_kara = data->graph[j].player;
                preflight_links += data->graph[j].preflight;
                break;
            }
        }
        if (route < ROUTE_MIXED) {
            if (!is_kara || (player != SPA_ID_INVALID && link->upstream != player)) {
                route = ROUTE_MIXED;
            } else {
                route = ROUTE_KARA;
                player = link->upstream;
            }
        }
    }
    /* Publish route and preflight readiness together. The extra bit is
     * internal to this helper and never appears in a KPW1 packet. */
    atomic_store_explicit(&data->route, route | (preflight_links >= 2 ? PREFLIGHT_STEREO_ACTIVE : 0),
                          memory_order_release);
}

static void remember_node(struct data *data, struct graph_node node)
{
    if (data->graph_size == MAX_NODES) {
        data->problem = "registry_limit";
        return;
    }
    data->graph[data->graph_size++] = node;
    update_route(data);
}

static void playback_info(void *userdata, const struct pw_link_info *info)
{
    struct playback_link *link = userdata;
    if (info->change_mask & PW_LINK_CHANGE_MASK_STATE) {
        link->active = info->state == PW_LINK_STATE_ACTIVE;
        link->known = true;
        update_route(link->owner);
    }
}

static const struct pw_link_events playback_events = { PW_VERSION_LINK_EVENTS, .info = playback_info };

static void watch_playback(struct data *data, uint32_t id, uint32_t upstream)
{
    /* A paused application can leave INIT links in the graph. Attribution
     * depends on active links carrying PCM, not the existence of a link. */
    for (size_t i = 0; i < MAX_PLAYBACK_LINKS; i++) {
        struct playback_link *link = &data->playback[i];
        if (link->proxy)
            continue;
        *link = (struct playback_link){ .owner = data, .id = id, .upstream = upstream };
        link->proxy = pw_registry_bind(data->registry, id, PW_TYPE_INTERFACE_Link, PW_VERSION_LINK, 0);
        if (!link->proxy || pw_link_add_listener(link->proxy, &link->listener, &playback_events, link) < 0)
            data->problem = "cannot_watch_playback_link";
        update_route(data);
        return;
    }
    data->problem = "playback_link_limit";
}

static void select_id(struct data *data, uint32_t *slot, uint32_t id)
{
    if (*slot != SPA_ID_INVALID && *slot != id)
        data->problem = "ambiguous_graph_port_or_node";
    *slot = id;
}

static void global(void *userdata, uint32_t id, uint32_t permissions, const char *type,
                   uint32_t version, const struct spa_dict *props)
{
    (void)permissions;
    (void)version;
    struct data *data = userdata;
    if (!props)
        return;
    if (equals(type, PW_TYPE_INTERFACE_Node)) {
        const char *name = spa_dict_lookup(props, PW_KEY_NODE_NAME);
        if (equals(name, data->mic_name))
            select_id(data, &data->mic, id);
        if (equals(name, data->sink_name))
            select_id(data, &data->sink, id);
        if (equals(name, data->filter_name))
            select_id(data, &data->own, id);
        remember_node(data, (struct graph_node){ .id = id, .player = kara_node(name),
            .preflight = data->preflight_player && equals(name, data->preflight_player) });
    } else if (equals(type, PW_TYPE_INTERFACE_Link)) {
        uint32_t input = id_value(props, PW_KEY_LINK_INPUT_NODE);
        uint32_t output = id_value(props, PW_KEY_LINK_OUTPUT_NODE);
        if (data->sink != SPA_ID_INVALID && input == data->sink)
            watch_playback(data, id, output);
        if (input == data->own && data->own != SPA_ID_INVALID) {
            uint32_t port = id_value(props, PW_KEY_LINK_INPUT_PORT);
            uint32_t source = id_value(props, PW_KEY_LINK_OUTPUT_PORT);
            bool expected = false;
            for (unsigned i = 0; i < CHANNELS; i++) {
                struct tap *tap = &data->tap[i];
                if (tap->own == port && tap->peer == source && tap->link)
                    expected = true;
            }
            if (!expected)
                data->problem = "unowned_input_link";
        }
    } else if (equals(type, PW_TYPE_INTERFACE_Port)) {
        uint32_t node = id_value(props, PW_KEY_NODE_ID);
        const char *direction = spa_dict_lookup(props, PW_KEY_PORT_DIRECTION);
        const char *name = spa_dict_lookup(props, PW_KEY_PORT_NAME);
        const char *channel = spa_dict_lookup(props, PW_KEY_AUDIO_CHANNEL);
        bool monitor = equals(spa_dict_lookup(props, PW_KEY_PORT_MONITOR), "true");
        if (node == SPA_ID_INVALID)
            return;
        for (unsigned i = 0; i < CHANNELS; i++) {
            struct tap *tap = &data->tap[i];
            if (node == data->own && equals(name, tap->name) && equals(direction, "in"))
                select_id(data, &tap->own, id);
            uint32_t source = i == 0 ? data->mic : data->sink;
            if (node == source && monitor == (i != 0) && equals(direction, "out")
                    && equals(channel, tap->channel))
                select_id(data, &tap->peer, id);
        }
    }
}

static void global_remove(void *userdata, uint32_t id)
{
    struct data *data = userdata;
    for (unsigned i = 0; i < CHANNELS; i++) {
        const struct tap *tap = &data->tap[i];
        if (id == tap->peer || id == tap->own || id == tap->link_id)
            data->problem = "tap_removed";
    }
    if (id == data->mic || id == data->sink || id == data->own)
        data->problem = "endpoint_removed";
    for (size_t i = 0; i < MAX_PLAYBACK_LINKS; i++) {
        struct playback_link *link = &data->playback[i];
        if (link->proxy && link->id == id) {
            pw_proxy_destroy((struct pw_proxy *)link->proxy);
            link->proxy = NULL;
            update_route(data);
            break;
        }
    }
    for (size_t i = 0; i < data->graph_size; i++) {
        if (data->graph[i].id == id) {
            data->graph[i] = data->graph[--data->graph_size];
            update_route(data);
            break;
        }
    }
}

static void link_info(void *userdata, const struct pw_link_info *info)
{
    struct tap *tap = userdata;
    struct data *data = tap->owner;
    tap->link_id = info->id;
    if (info->change_mask & PW_LINK_CHANGE_MASK_STATE) {
        tap->active = info->state == PW_LINK_STATE_ACTIVE;
        if (info->state <= PW_LINK_STATE_UNLINKED || (data->armed && !tap->active)) {
            data->problem = "tap_inactive";
            return;
        }
    }
    if (info->props && (equals(spa_dict_lookup(info->props, PW_KEY_LINK_ASYNC), "true")
                     || equals(spa_dict_lookup(info->props, PW_KEY_LINK_FEEDBACK), "true"))) {
        data->problem = "tap_not_synchronous";
        return;
    }
    /* PipeWire can report a negotiated format on the ACTIVE event with only
     * the state change bit set. The attached format is still authoritative. */
    if (tap->active || (info->change_mask & PW_LINK_CHANGE_MASK_FORMAT)) {
        uint32_t media, subtype;
        struct spa_audio_info_dsp dsp = { 0 };
        tap->format_known = info->format != NULL;
        if (!tap->format_known) {
            if (data->armed)
                data->problem = "tap_format_lost";
            return;
        }
        if (spa_format_parse(info->format, &media, &subtype) < 0 || media != SPA_MEDIA_TYPE_audio
                || subtype != SPA_MEDIA_SUBTYPE_dsp || spa_format_audio_dsp_parse(info->format, &dsp) < 0
                || dsp.format != SPA_AUDIO_FORMAT_DSP_F32) {
            data->problem = "tap_format_not_f32_dsp";
            return;
        }
    }
}

static void endpoint_info(void *userdata, const struct pw_node_info *info)
{
    struct endpoint *endpoint = userdata;
    if (info->change_mask & PW_NODE_CHANGE_MASK_STATE)
        endpoint->running = info->state == PW_NODE_STATE_RUNNING;
    if ((info->change_mask & PW_NODE_CHANGE_MASK_PROPS) && info->props) {
        const struct spa_dict *props = info->props;
        if (equals(spa_dict_lookup(props, PW_KEY_NODE_ASYNC), "true"))
            endpoint->owner->problem = "tap_not_synchronous";
        endpoint->driver = id_value(props, PW_KEY_NODE_DRIVER_ID);
        endpoint->can_drive = equals(spa_dict_lookup(props, PW_KEY_NODE_DRIVER), "true");
    }
}

static void core_done(void *userdata, uint32_t id, int seq)
{
    if (id == PW_ID_CORE)
        ((struct data *)userdata)->completed_sync = seq;
}

static void core_error(void *userdata, uint32_t id, int seq, int res, const char *message)
{
    (void)id; (void)seq; (void)res;
    fprintf(stderr, "PipeWire: %s\n", message);
    ((struct data *)userdata)->problem = "pipewire_error";
}

static void state_changed(void *userdata, enum pw_filter_state old, enum pw_filter_state state,
                          const char *error)
{
    (void)old;
    struct data *data = userdata;
    data->state = state;
    if (state == PW_FILTER_STATE_ERROR || (data->armed && state != PW_FILTER_STATE_STREAMING)) {
        if (error)
            fprintf(stderr, "PipeWire: %s\n", error);
        data->problem = "filter_inactive";
    }
}

static const struct pw_registry_events registry_events = {
    PW_VERSION_REGISTRY_EVENTS, .global = global, .global_remove = global_remove,
};
static const struct pw_core_events core_events = {
    PW_VERSION_CORE_EVENTS, .done = core_done, .error = core_error,
};
static const struct pw_filter_events filter_events = {
    PW_VERSION_FILTER_EVENTS, .state_changed = state_changed, .process = process,
};
static const struct pw_link_events link_events = { PW_VERSION_LINK_EVENTS, .info = link_info };
static const struct pw_node_events endpoint_events = { PW_VERSION_NODE_EVENTS, .info = endpoint_info };

static bool watch_endpoint(struct data *data, struct pw_registry *registry, unsigned index, uint32_t id)
{
    struct endpoint *endpoint = &data->endpoint[index];
    if (endpoint->proxy)
        pw_proxy_destroy((struct pw_proxy *)endpoint->proxy);
    *endpoint = (struct endpoint){ .owner = data, .id = id, .driver = SPA_ID_INVALID };
    endpoint->proxy = pw_registry_bind(registry, id, PW_TYPE_INTERFACE_Node, PW_VERSION_NODE, 0);
    return endpoint->proxy && pw_node_add_listener(endpoint->proxy, &endpoint->listener,
                                                  &endpoint_events, endpoint) >= 0;
}

static uint32_t common_driver(const struct data *data)
{
    const struct endpoint *mic = &data->endpoint[0], *sink = &data->endpoint[1];
    if (data->enabled_taps != 7) {
        const struct endpoint *endpoint = data->enabled_taps == 1 ? mic : sink;
        if (!endpoint->running)
            return SPA_ID_INVALID;
        return endpoint->driver == SPA_ID_INVALID && endpoint->can_drive ? endpoint->id : endpoint->driver;
    }
    if (!mic->running || !sink->running)
        return SPA_ID_INVALID;
    if (mic->driver == sink->driver)
        return mic->driver;
    /* The elected driver can omit its own assignment. Accept that case only
     * with the peer's explicit assignment to it. process() independently
     * checks the same ID in SPA_IO_Position before publishing any PCM. */
    if (mic->driver == SPA_ID_INVALID && mic->can_drive && sink->driver == mic->id)
        return sink->driver;
    if (sink->driver == SPA_ID_INVALID && sink->can_drive && mic->driver == sink->id)
        return mic->driver;
    return SPA_ID_INVALID;
}

static bool make_link(struct data *data, struct tap *tap)
{
    char input[16], output[16];
    snprintf(input, sizeof(input), "%u", tap->own);
    snprintf(output, sizeof(output), "%u", tap->peer);
    struct pw_properties *props = pw_properties_new(PW_KEY_LINK_OUTPUT_PORT, output,
        PW_KEY_LINK_INPUT_PORT, input, PW_KEY_OBJECT_LINGER, "false", PW_KEY_LINK_PASSIVE,
        data->enabled_taps == 6 ? "true" : "false", NULL);
    if (!props)
        return false;
    tap->link = pw_core_create_object(data->core, "link-factory", PW_TYPE_INTERFACE_Link,
                                     PW_VERSION_LINK, &props->dict, 0);
    pw_properties_free(props);
    return tap->link && pw_link_add_listener(tap->link, &tap->listener, &link_events, tap) >= 0;
}

static uint64_t now_ms(void)
{
    struct timespec time = { 0 };
    clock_gettime(CLOCK_MONOTONIC, &time);
    return (uint64_t)time.tv_sec * 1000 + (uint64_t)time.tv_nsec / 1000000;
}

static void write_packets(struct data *data, size_t *offset)
{
    unsigned head = atomic_load_explicit(&data->produced, memory_order_acquire);
    unsigned tail = atomic_load_explicit(&data->consumed, memory_order_relaxed);
    while (tail != head) {
        const struct packet *slot = &data->ring[tail % RING];
        ssize_t written = write(STDOUT_FILENO, slot->wire + *offset, slot->size - *offset);
        if (written < 0) {
            if (errno != EAGAIN && errno != EINTR)
                data->problem = "pcm_pipe_failed";
            return;
        }
        *offset += (size_t)written;
        if (*offset != slot->size)
            return;
        *offset = 0;
        atomic_store_explicit(&data->consumed, ++tail, memory_order_release);
    }
}

int main(int argc, char **argv)
{
    bool preflight = argc == 6 && equals(argv[4], "--preflight-player") && argv[5][0] != '\0';
    bool monitor = argc == 5 && equals(argv[4], "--passive-monitor");
    bool witness = argc == 5 && equals(argv[4], "--witness-only");
    if ((argc != 4 && !preflight && !monitor && !witness)
            || (!equals(argv[3], "FL") && !equals(argv[3], "FR") && !equals(argv[3], "MONO"))) {
        fprintf(stderr, "usage: %s MIC_NODE SINK_NODE FL|FR|MONO [--preflight-player NODE | --passive-monitor | --witness-only]\n", argv[0]);
        return 2;
    }
    struct data *data = calloc(1, sizeof(*data));
    if (!data)
        return 2;
    atomic_init(&data->ready, 0);
    atomic_init(&data->expected_driver, SPA_ID_INVALID);
    atomic_init(&data->fault, FAULT_NONE);
    atomic_init(&data->produced, 0);
    atomic_init(&data->consumed, 0);
    atomic_init(&data->route, ROUTE_NONE);
    data->enabled_taps = monitor ? 6 : witness ? 1 : 7;
    data->mic_name = monitor ? NULL : argv[1];
    data->sink_name = witness ? NULL : argv[2];
    data->preflight_player = preflight ? argv[5] : NULL;
    data->mic = data->sink = data->own = SPA_ID_INVALID;
    snprintf(data->filter_name, sizeof(data->filter_name), "kara-pipewire-witness.%ld", (long)getpid());
    const char *names[] = { "witness", "monitor-left", "monitor-right" };
    const char *channels[] = { argv[3], "FL", "FR" };
    for (unsigned i = 0; i < CHANNELS; i++)
        data->tap[i] = (struct tap){ .owner = data, .name = names[i], .channel = channels[i],
                                   .peer = SPA_ID_INVALID, .own = SPA_ID_INVALID, .link_id = SPA_ID_INVALID };
    signal(SIGPIPE, SIG_IGN);
    signal(SIGINT, stop_signal);
    signal(SIGTERM, stop_signal);
    pw_init(NULL, NULL);
    struct pw_main_loop *main_loop = pw_main_loop_new(NULL);
    struct pw_loop *loop = main_loop ? pw_main_loop_get_loop(main_loop) : NULL;
    struct pw_context *context = loop ? pw_context_new(loop, NULL, 0) : NULL;
    data->core = context ? pw_context_connect(context, NULL, 0) : NULL;
    struct pw_registry *registry = data->core ? pw_core_get_registry(data->core, PW_VERSION_REGISTRY, 0) : NULL;
    data->registry = registry;
    struct spa_hook core_hook, registry_hook, filter_hook;
    int stdout_flags = fcntl(STDOUT_FILENO, F_GETFL);
    if (!registry || stdout_flags < 0 || fcntl(STDOUT_FILENO, F_SETFL, stdout_flags | O_NONBLOCK) < 0) {
        data->problem = "pipewire_setup";
        goto cleanup;
    }
    pw_core_add_listener(data->core, &core_hook, &core_events, data);
    pw_registry_add_listener(registry, &registry_hook, &registry_events, data);
    data->filter = pw_filter_new(data->core, data->filter_name,
        pw_properties_new(PW_KEY_NODE_NAME, data->filter_name, PW_KEY_NODE_AUTOCONNECT, "false",
                          PW_KEY_NODE_WANT_DRIVER, "true", PW_KEY_MEDIA_TYPE, "Audio", NULL));
    if (!data->filter) {
        data->problem = "filter_setup";
        goto cleanup;
    }
    pw_filter_add_listener(data->filter, &filter_hook, &filter_events, data);
    for (unsigned i = 0; i < CHANNELS; i++) {
        if (!(data->enabled_taps & (1u << i)))
            continue;
        struct tap *tap = &data->tap[i];
        tap->port = pw_filter_add_port(data->filter, PW_DIRECTION_INPUT,
            PW_FILTER_PORT_FLAG_MAP_BUFFERS, 1, pw_properties_new(PW_KEY_PORT_NAME, tap->name,
            PW_KEY_FORMAT_DSP, "32 bit float mono audio", NULL), NULL, 0);
        if (!tap->port) {
            data->problem = "port_setup";
            goto cleanup;
        }
    }
    if (pw_filter_connect(data->filter, PW_FILTER_FLAG_RT_PROCESS, NULL, 0) < 0) {
        data->problem = "filter_connect";
        goto cleanup;
    }
    int sync = pw_core_sync(data->core, PW_ID_CORE, 0);
    if (sync < 0) {
        data->problem = "registry_sync_failed";
        goto cleanup;
    }
    uint64_t deadline = now_ms() + 6000;
    uint64_t next_bind = 0;
    size_t offset = 0;
    bool linked = false;
    pw_loop_enter(loop);
    while (!stop && !data->problem) {
        int res = pw_loop_iterate(loop, 5);
        if (res < 0 && res != -EINTR) {
            data->problem = "pipewire_loop";
            break;
        }
        if (!linked && data->completed_sync == sync && data->state == PW_FILTER_STATE_PAUSED) {
            bool found = true;
            for (unsigned i = 0; i < CHANNELS; i++)
                if (data->enabled_taps & (1u << i))
                    found &= data->tap[i].own != SPA_ID_INVALID && data->tap[i].peer != SPA_ID_INVALID;
            if (found) {
                linked = true;
                for (unsigned i = 0; i < CHANNELS; i++)
                    if ((data->enabled_taps & (1u << i)) && !make_link(data, &data->tap[i]))
                        data->problem = "cannot_link_tap";
            }
        }
        uint32_t driver = common_driver(data);
        uint64_t now = now_ms();
        bool taps_ready = linked && data->state == PW_FILTER_STATE_STREAMING;
        for (unsigned i = 0; i < CHANNELS; i++)
            if (data->enabled_taps & (1u << i))
                taps_ready &= data->tap[i].active && data->tap[i].format_known;
        if (!data->armed && taps_ready && !data->problem) {
            if (driver != SPA_ID_INVALID) {
                data->armed = true;
                atomic_store_explicit(&data->expected_driver, driver, memory_order_relaxed);
                atomic_store_explicit(&data->ready, 1, memory_order_release);
            } else if (now >= next_bind) {
                /* Joining the taps can reassign an already-running sink.
                 * PipeWire 1.6.11 can update its properties without notifying
                 * the follower's existing subscriber. Bind after the links
                 * are active and retry fresh snapshots while ALSA starts.
                 * Keep the successful subscriptions for state changes. The
                 * live synchronous taps share the scheduled subgraph, and
                 * the RT clock check guards its driver for every cycle. */
                if (((data->enabled_taps & 1) && !watch_endpoint(data, registry, 0, data->mic))
                        || ((data->enabled_taps & 6) && !watch_endpoint(data, registry, 1, data->sink)))
                    data->problem = "cannot_watch_endpoint";
                next_bind = now + 150;
            }
        }
        if (data->armed && driver != atomic_load_explicit(&data->expected_driver, memory_order_relaxed))
            data->problem = "endpoint_clock_changed";
        unsigned fault = atomic_load_explicit(&data->fault, memory_order_acquire);
        if (fault)
            data->problem = fault == FAULT_CLOCK ? "graph_clock_unusable" :
                            fault == FAULT_BUFFER ? "pcm_missing_or_invalid" : "pcm_queue_overflow";
        if (!data->problem)
            write_packets(data, &offset);
        if (!data->problem && now > deadline && !atomic_load_explicit(&data->produced, memory_order_acquire))
            data->problem = !linked ? "named_ports_unavailable"
                           : driver == SPA_ID_INVALID ? "common_driver_unconfirmed" : "no_complete_common_cycle";
    }
    pw_loop_leave(loop);
cleanup:
    data->armed = false;
    atomic_store_explicit(&data->ready, 0, memory_order_release);
    for (unsigned i = 0; i < CHANNELS; i++)
        if (data->tap[i].link)
            pw_proxy_destroy((struct pw_proxy *)data->tap[i].link);
    for (unsigned i = 0; i < 2; i++)
        if (data->endpoint[i].proxy)
            pw_proxy_destroy((struct pw_proxy *)data->endpoint[i].proxy);
    for (unsigned i = 0; i < MAX_PLAYBACK_LINKS; i++)
        if (data->playback[i].proxy)
            pw_proxy_destroy((struct pw_proxy *)data->playback[i].proxy);
    if (data->filter)
        pw_filter_destroy(data->filter);
    if (registry)
        pw_proxy_destroy((struct pw_proxy *)registry);
    if (data->core)
        pw_core_disconnect(data->core);
    if (context)
        pw_context_destroy(context);
    if (main_loop)
        pw_main_loop_destroy(main_loop);
    if (data->problem)
        fprintf(stderr, "kara_pipewire_witness: %s\n", data->problem);
    int status = data->problem ? 1 : 0;
    pw_deinit();
    free(data);
    return status;
}
