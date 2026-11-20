package cn.tedu.agt.ops;

import cn.tedu.agt.agent.client.AgentClient;
import cn.tedu.agt.agent.config.AgentProperties;
import cn.tedu.agt.ops.service.impl.OpsServiceImpl;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * E-9 指标合并层（{@code OpsServiceImpl.metrics}）单测。
 *
 * <p>这里守的是三件在真机上会静默出问题的事：</p>
 *
 * <ol>
 *   <li><b>两段拼起来不能撞名</b>。Java 段（{@code agt_java_*}）与 Python 段（{@code agt_*}）
 *       是两份独立生成的文本，谁多写一个同名族，Prometheus 就会以
 *       {@code duplicate sample for timestamp} 让<b>整次抓取失败</b>——症状是"所有指标都没了"，
 *       而人第一反应会去怀疑网络。所以本测试<b>解析拼接后的完整文本</b>并检查重复。</li>
 *   <li><b>新的出站调用必须带服务间密钥</b>（S-1）。漏了不会有编译错误，
 *       表现是"指标端点永远只有 Java 段"，很容易被当成 Python 侧的问题。</li>
 *   <li><b>Python 不可达时必须降级而不是抛</b>。指标端点自己成为故障点是最坏的设计：
 *       出问题时恰好没有指标可看。</li>
 * </ol>
 *
 * <p>用 JDK 自带的 {@code com.sun.net.httpserver} 起真实本地服务（同 E-5 契约测试的做法），
 * 因为要断言的正是"线路上真的带了这个头、这个查询串"。零新依赖、全程 127.0.0.1、断网可跑。</p>
 */
class OpsMetricsServiceTest {

    private static final String EXPECTED_KEY = "metrics-test-shared-key";

    private static final String PYTHON_SECTION = """
            # HELP agt_python_up 智能体（Python）进程存活
            # TYPE agt_python_up gauge
            agt_python_up 1
            # HELP agt_chat_requests_total 对话请求总数
            # TYPE agt_chat_requests_total counter
            agt_chat_requests_total{mode="REACT",intent_source="rule",model="deepseek-flash"} 7
            """;

    private HttpServer server;
    private final AtomicReference<String> lastQuery = new AtomicReference<>();
    private final AtomicReference<String> lastKey = new AtomicReference<>();
    private final List<String> lastPaths = new ArrayList<>();
    private volatile String body = PYTHON_SECTION;
    private volatile String contentType = "text/plain; version=0.0.4; charset=utf-8";

    private OpsServiceImpl service;

    @BeforeEach
    void setUp() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", ex -> {
            lastPaths.add(ex.getRequestURI().getPath());
            lastQuery.set(ex.getRequestURI().getQuery());
            lastKey.set(ex.getRequestHeaders().getFirst("X-Agent-Key"));
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            ex.getResponseHeaders().add("Content-Type", contentType);
            ex.sendResponseHeaders(200, bytes.length);
            try (OutputStream out = ex.getResponseBody()) {
                out.write(bytes);
            }
        });
        server.start();

        AgentProperties properties = new AgentProperties();
        properties.setBaseUrl("http://127.0.0.1:" + server.getAddress().getPort());
        properties.setSharedKey(EXPECTED_KEY);
        properties.setConnectTimeout(2_000);
        properties.setReadTimeout(5_000);
        properties.setOpsReadTimeout(2_000);

        service = new OpsServiceImpl();
        ReflectionTestUtils.setField(service, "agentClient",
                new AgentClient(properties, new ObjectMapper()));
    }

    @AfterEach
    void tearDown() {
        server.stop(0);
    }

    // ------------------------------------------------------------------ 工具

    /** 解析拼接后的文本：返回 (族名集合, 错误列表)。校验规则同 PrometheusTextTest。 */
    private static List<String> validate(String text) {
        List<String> errors = new ArrayList<>();
        Set<String> helps = new HashSet<>();
        Set<String> types = new HashSet<>();
        Set<String> series = new HashSet<>();
        for (String line : text.split("\n")) {
            if (line.isBlank()) {
                continue;
            }
            if (line.startsWith("#")) {
                String rest = line.substring(1).strip();
                if (!rest.startsWith("HELP ") && !rest.startsWith("TYPE ")) {
                    continue;
                }
                String[] parts = rest.split(" ", 3);
                String name = parts[1];
                if (parts[0].equals("HELP")) {
                    if (!helps.add(name)) {
                        errors.add("HELP 重复（拼接撞名）：" + name);
                    }
                    if (types.contains(name)) {
                        errors.add("HELP 出现在 TYPE 之后：" + name);
                    }
                } else {
                    if (!types.add(name)) {
                        errors.add("TYPE 重复（拼接撞名）：" + name);
                    }
                    if (!helps.contains(name)) {
                        errors.add("TYPE 缺少前置 HELP：" + name);
                    }
                }
                continue;
            }
            if (!line.matches("[a-zA-Z_:][a-zA-Z0-9_:]*(\\{.*\\})? \\S+")) {
                errors.add("样本行无法解析：" + line);
                continue;
            }
            if (!series.add(line)) {
                errors.add("重复序列：" + line);
            }
        }
        return errors;
    }

    private static String lineWith(String text, String prefix) {
        for (String line : text.split("\n")) {
            if (line.startsWith(prefix)) {
                return line;
            }
        }
        throw new AssertionError("未找到以 " + prefix + " 开头的行");
    }

    // ------------------------------------------------------------------ 合并

    @Test
    @DisplayName("两段合并后无撞名、无重复序列，且两端指标都在")
    void mergesBothSectionsWithoutDuplicates() {
        String out = service.metrics(24, false);

        assertThat(validate(out)).isEmpty();
        assertThat(out).contains("agt_java_up 1");
        assertThat(out).contains("agt_python_up 1");
        assertThat(out).contains("agt_java_heap_used_bytes");
        assertThat(out).contains("agt_chat_requests_total");
        assertThat(lineWith(out, "agt_java_python_section_ok")).isEqualTo("agt_java_python_section_ok 1");
    }

    @Test
    @DisplayName("输出是纯文本，不是本项目的 JSON 信封（包成 {code,data} 等于端点不存在）")
    void outputIsNotJsonWrapped() {
        String out = service.metrics(24, false);
        assertThat(out.stripLeading()).doesNotStartWith("{");
        assertThat(out).doesNotContain("\"code\"");
        assertThat(out).doesNotContain("\"data\"");
        assertThat(out).endsWith("\n");
    }

    @Test
    @DisplayName("Python 段原样透传（不加工：加工会让两个端点看到的不一样）")
    void pythonSectionIsPassedThroughVerbatim() {
        String out = service.metrics(24, false);
        assertThat(out).contains(PYTHON_SECTION.strip());
    }

    @Test
    @DisplayName("window / fresh 透传到 Python 侧")
    void forwardsWindowAndFresh() {
        service.metrics(24, false);
        assertThat(lastQuery.get()).isEqualTo("window=24");
        service.metrics(6, true);
        assertThat(lastQuery.get()).isEqualTo("window=6&fresh=true");
        assertThat(lastPaths).allMatch("/v1/ops/metrics"::equals);
    }

    @Test
    @DisplayName("新的出站调用带 X-Agent-Key（S-1：漏了就只有 Java 段，且不会有编译错误）")
    void carriesSharedKey() {
        service.metrics(24, false);
        assertThat(lastKey.get()).isEqualTo(EXPECTED_KEY);
    }

    @Test
    @DisplayName("间隔抓取不会因为缓存而拿到旧文本——本层不缓存，缓存归 Python 侧管")
    void doesNotCacheOnJavaSide() {
        service.metrics(24, false);
        body = "# HELP agt_python_up x\n# TYPE agt_python_up gauge\nagt_python_up 0\n";
        assertThat(service.metrics(24, false)).contains("agt_python_up 0");
    }

    // ------------------------------------------------------------------ 降级

    @Test
    @DisplayName("Python 不可达：仍返回 Java 段 + 标记 0 + 注释说明原因，且不抛异常")
    void pythonUnreachableStillReturnsJavaSection() {
        server.stop(0);

        // 直接调用即可：**它一旦抛异常本测试就红**，这正是要守的性质
        String out = service.metrics(24, false);

        assertThat(validate(out)).isEmpty();
        assertThat(out).contains("agt_java_up 1");
        assertThat(out).contains("agt_java_heap_used_bytes");
        assertThat(lineWith(out, "agt_java_python_section_ok")).isEqualTo("agt_java_python_section_ok 0");
        assertThat(out).contains("# Python 段缺失：");
        // ⚠️ 断言必须精确到"样本行的开头"：Java 段的 HELP 文案里就提到了 `agt_python_up`
        // （"它突然变小 = 进程被重启过"那段），用 doesNotContain("agt_python_up") 会误报。
        assertThat(out).doesNotContain("\nagt_python_up ");
    }

    @Test
    @DisplayName("Python 返回空体也算失败（空体和\"本来就没数据\"长得一样，必须显式标记）")
    void blankPythonBodyIsTreatedAsFailure() {
        body = "   \n";
        String out = service.metrics(24, false);
        assertThat(lineWith(out, "agt_java_python_section_ok")).isEqualTo("agt_java_python_section_ok 0");
        assertThat(out).contains("空响应");
        assertThat(out).contains("agt_java_up 1");
    }

    @Test
    @DisplayName("Python 返回坏数据（非 200）也不外抛：只降级为\"Python 段缺失\"")
    void errorResponseDoesNotBubbleUp() {
        server.removeContext("/");
        server.createContext("/", ex -> {
            ex.sendResponseHeaders(500, -1);
            ex.close();
        });
        String out = service.metrics(24, false);
        assertThat(lineWith(out, "agt_java_python_section_ok")).isEqualTo("agt_java_python_section_ok 0");
        assertThat(out).contains("agt_java_up 1");
    }
}
