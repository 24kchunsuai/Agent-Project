package cn.tedu.agt.base.audit;

import cn.tedu.agt.governance.client.GovernanceClient;
import jakarta.servlet.http.HttpServletRequest;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

/**
 * 操作审计补录出口（E-8，判据 C5）。
 *
 * <p>为什么需要这一层：审计表只有一张，在治理域（Python 侧 SQLite）。但有几类动作
 * <strong>发生在 Java 侧</strong>——登录成功/失败、登出——它们根本没经过 Python。
 * 规约 E-8 明确要求"复用治理域既有 {@code audit()} 机制（不新建第二套）"，
 * 所以做法是 Java 把这几类事件<strong>补录</strong>进同一张表，而不是再养一套存储。</p>
 *
 * <p><b>失败语义（关键）</b>：补录失败只打 WARN，<strong>绝不向上抛</strong>。
 * 审计是"事后追责"能力，不能让它变成"登录失败"的原因——一次 Python 抖动
 * 不该把所有人挡在门外。这也是为什么这里连异常类型都不细分，一律吞。</p>
 *
 * <p><b>不记内容</b>：{@code detail} 只放"为什么"（如 {@code 密码错误}），
 * 不放密码、不放 token——与埋点铁律（元数据全量记、内容不落库）一致。</p>
 */
@Slf4j
@Component
public class AuditReporter {

    // ---- 动作常量（与 Python 侧 GovAction 逐字一致，两端都用于筛选）----
    /** 登录成功；target=user:&lt;用户名&gt; */
    public static final String LOGIN_OK = "LOGIN_OK";
    /** 登录失败；target=user:&lt;用户名&gt; */
    public static final String LOGIN_FAIL = "LOGIN_FAIL";
    /** 登出；target=user:&lt;用户名&gt; */
    public static final String LOGOUT = "LOGOUT";

    public static final String RESULT_SUCCESS = "SUCCESS";
    public static final String RESULT_FAILED = "FAILED";

    /** 操作审计里 {@code target} 的统一前缀，与 Python 侧 TARGET_USER 一致。 */
    private static final String TARGET_USER = "user:";

    /** detail 上限（Python 侧 Pydantic 限 1000，这里留余量）。 */
    private static final int DETAIL_MAX = 400;

    private final GovernanceClient governanceClient;

    public AuditReporter(GovernanceClient governanceClient) {
        this.governanceClient = governanceClient;
    }

    // ------------------------------------------------------------ 对外便捷方法

    /** 登录成功。 */
    public void loginOk(String username, String ip) {
        report(username, LOGIN_OK, TARGET_USER + safe(username), RESULT_SUCCESS, null, ip);
    }

    /**
     * 登录失败。
     *
     * @param reason 失败原因（<b>不含密码</b>），如"用户名或密码错误"、"上游不可用"
     */
    public void loginFail(String username, String reason, String ip) {
        report(username, LOGIN_FAIL, TARGET_USER + safe(username), RESULT_FAILED, reason, ip);
    }

    /** 登出。 */
    public void logout(String username, String ip) {
        report(username, LOGOUT, TARGET_USER + safe(username), RESULT_SUCCESS, null, ip);
    }

    // ------------------------------------------------------------ 底层出口

    /**
     * 补录一条审计。**永不抛异常**。
     *
     * @param actor  操作人（未登录场景由调用方给匿名标识，如 {@code anonymous}）
     * @param action 动作常量
     * @param target 统一指向（{@code user:admin} / {@code tool:dispatch} / …）
     * @param result {@link #RESULT_SUCCESS} / {@link #RESULT_FAILED}
     * @param detail 附注（不含敏感内容），可为 null
     * @param ip     来源 IP，可为 null
     */
    public void report(String actor, String action, String target,
                       String result, String detail, String ip) {
        try {
            governanceClient.auditEvent(
                    safe(actor), action, target, result, truncate(detail), blankToNull(ip));
        } catch (Exception e) {
            // 审计补录失败不能反过来打断登录/登出
            log.warn("操作审计补录失败（已忽略）: action={}, actor={}, err={}",
                    action, actor, e.getMessage());
        }
    }

    // ------------------------------------------------------------ 工具

    /**
     * 取客户端 IP（网关场景优先 X-Forwarded-For 的第一跳）。
     *
     * <p>只做记录用途，不做信任判定——所以不校验代理链，取到第一个非空值即可。</p>
     */
    public static String clientIp(HttpServletRequest request) {
        if (request == null) {
            return null;
        }
        String forwarded = request.getHeader("X-Forwarded-For");
        if (StringUtils.hasText(forwarded)) {
            String first = forwarded.split(",")[0].trim();
            if (StringUtils.hasText(first)) {
                return first;
            }
        }
        String realIp = request.getHeader("X-Real-IP");
        if (StringUtils.hasText(realIp)) {
            return realIp.trim();
        }
        return blankToNull(request.getRemoteAddr());
    }

    private static String safe(String value) {
        return StringUtils.hasText(value) ? value.trim() : "anonymous";
    }

    private static String blankToNull(String value) {
        return StringUtils.hasText(value) ? value.trim() : null;
    }

    private static String truncate(String detail) {
        if (detail == null) {
            return null;
        }
        String trimmed = detail.trim();
        return trimmed.length() <= DETAIL_MAX ? trimmed : trimmed.substring(0, DETAIL_MAX);
    }
}
