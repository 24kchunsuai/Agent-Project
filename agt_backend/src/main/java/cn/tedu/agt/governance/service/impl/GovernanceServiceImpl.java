package cn.tedu.agt.governance.service.impl;

import cn.tedu.agt.agent.pojo.AgentApiResponse;
import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.auth.LoginUser;
import cn.tedu.agt.base.exception.ConfirmRequiredException;
import cn.tedu.agt.base.exception.ServiceException;
import cn.tedu.agt.base.response.StatusCode;
import cn.tedu.agt.governance.client.GovernanceClient;
import cn.tedu.agt.governance.service.GovernanceService;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * 语料治理服务的实现：权限收口 + 参数收敛 + 错误翻译 + 确认门透传。
 *
 * <p>本类不碰 SQLite、不碰 Milvus——治理数据是 Python 的私有账本（规约 §13.2），
 * Java 只是前端入口与权限守门人。所有校验都在<b>调用下游之前</b>完成，
 * 避免把越权请求变成一次无效的下游写操作与审计噪音。</p>
 */
@Slf4j
@Service
public class GovernanceServiceImpl implements GovernanceService {

    /** 管理员角色标识，与 jsdstu-3 的 RoleEnum 一致（与 KbServiceImpl 同口径） */
    private static final String ROLE_ADMIN = "admin";

    /** 分页默认值与上限，与 Python 端 governance 接口口径一致 */
    private static final int DEFAULT_PAGE_SIZE = 20;
    private static final int MAX_PAGE_SIZE = 200;

    /** 合法状态值，与 Python 端 `/governance/docs` 的校验保持一致（早拦一次，省一次往返） */
    private static final java.util.Set<String> VALID_STATES =
            java.util.Set.of("ACTIVE", "DISABLED", "RECYCLED");

    /** 合法工单状态与类型，与 Python 端 `/governance/tickets` 的校验一致（早拦一次，省一次往返） */
    private static final java.util.Set<String> VALID_TICKET_STATUS =
            java.util.Set.of("OPEN", "DISMISSED", "DONE", "CONFIRMED");
    private static final java.util.Set<String> VALID_TICKET_TYPES =
            java.util.Set.of("DUPLICATE", "STALE", "LOW_QUALITY");

    /** 手动对账的 operator 兜底值：审计里要能区分"人点的"与"定时任务跑的" */
    private static final String FALLBACK_OPERATOR = "manual";

    @Autowired
    private GovernanceClient governanceClient;

    // ------------------------------------------------------------ 只读视图（管理员）

    @Override
    public Object overview(DroneSession session) {
        requireAdmin(session, "治理概览");
        return unwrap(governanceClient.overview(), "治理概览查询");
    }

    @Override
    public Object audit(DroneSession session, String operator, String action, String docId,
                        String target, String from, String to, Integer page, Integer pageSize) {
        requireAdmin(session, "审计查询");
        return unwrap(governanceClient.audit(
                        operator, action, docId, target, from, to,
                        normalizePage(page), normalizePageSize(pageSize)),
                "审计查询");
    }

    @Override
    public Object auditExport(DroneSession session, String operator, String action, String docId,
                              String target, String from, String to) {
        requireAdmin(session, "审计导出");
        return unwrap(governanceClient.auditExport(operator, action, docId, target, from, to),
                "审计导出");
    }

    @Override
    public Object docs(DroneSession session, String state, Integer page, Integer pageSize) {
        requireAdmin(session, "治理登记列表");
        String normalized = null;
        if (StringUtils.hasText(state)) {
            normalized = state.trim().toUpperCase();
            if (!VALID_STATES.contains(normalized)) {
                throw new ServiceException(StatusCode.VALIDATE_ERROR,
                        "state 只支持 ACTIVE / DISABLED / RECYCLED");
            }
        }
        return unwrap(governanceClient.docs(normalized, normalizePage(page), normalizePageSize(pageSize)),
                "治理登记列表查询");
    }

    @Override
    public Object disabled(DroneSession session, Integer page, Integer pageSize) {
        requireAdmin(session, "停用区列表");
        return unwrap(governanceClient.disabled(normalizePage(page), normalizePageSize(pageSize)),
                "停用区列表查询");
    }

    @Override
    public Object config(DroneSession session) {
        requireAdmin(session, "治理配置");
        return unwrap(governanceClient.config(), "治理配置查询");
    }

    @Override
    public Object tasks(DroneSession session) {
        requireAdmin(session, "常驻任务状态");
        return unwrap(governanceClient.tasks(), "常驻任务状态查询");
    }

    @Override
    public Object versions(String docId, DroneSession session) {
        requireAdmin(session, "版本链查询");
        if (!StringUtils.hasText(docId)) {
            throw new ServiceException(StatusCode.VALIDATE_ERROR, "文档标识不能为空");
        }
        return unwrap(governanceClient.versions(docId), "版本链查询");
    }

    // ------------------------------------------------------------ 生命周期操作

    @Override
    public Object disable(String docId, DroneSession session) {
        LoginUser user = requireLogin(session);
        return unwrap(governanceClient.disable(docId, user.getUsername(), isAdmin(session)),
                "文档停用");
    }

    @Override
    public Object enable(String docId, DroneSession session) {
        LoginUser user = requireLogin(session);
        return unwrap(governanceClient.enable(docId, user.getUsername(), isAdmin(session)),
                "文档启用");
    }

    /**
     * 版本回滚。管理员专属，且过确认门。
     *
     * <p>权限先于下游判定：非管理员直接 4011，不产生一次无效回滚调用与审计噪音。
     * 这一点与 Python 侧的顺序一致（Python 也是先判 admin 再判 confirm）。</p>
     *
     * <p>targetVersion 为 null = "退回上一版"（Python 取版本号最大的历史快照），
     * 不在 Java 层做默认值填充——版本链的排序语义归 Python 账本，Java 猜一个数字
     * 反而可能指向错误的目标版本。</p>
     */
    @Override
    public Object rollback(String docId, Integer versionNo, boolean confirm,
                           String confirmDocName, DroneSession session) {
        LoginUser user = requireAdmin(session, "版本回滚");
        if (!StringUtils.hasText(docId)) {
            throw new ServiceException(StatusCode.VALIDATE_ERROR, "文档标识不能为空");
        }
        if (versionNo != null && versionNo < 1) {
            throw new ServiceException(StatusCode.VALIDATE_ERROR, "版本号必须为正整数");
        }
        return unwrap(governanceClient.rollback(docId, versionNo, confirm, confirmDocName,
                        user.getUsername(), isAdmin(session)),
                "版本回滚");
    }

    @Override
    public Object reconcileRun(DroneSession session, boolean dryRun) {
        LoginUser user = requireAdmin(session, "向量对账");
        // operator 取真实登录名：审计要能回答"这轮对账是谁触发的"
        return unwrap(governanceClient.reconcileRun(dryRun, operatorOf(user)), "向量对账");
    }

    // ------------------------------------------------------------ G4：巡检工单与 TTL

    @Override
    public Object tickets(DroneSession session, String status, String type,
                          Integer page, Integer pageSize) {
        requireAdmin(session, "工单列表");
        return unwrap(governanceClient.tickets(
                        normalizeEnum(status, VALID_TICKET_STATUS,
                                "status 只支持 OPEN / DISMISSED / DONE"),
                        normalizeEnum(type, VALID_TICKET_TYPES,
                                "type 只支持 DUPLICATE / STALE / LOW_QUALITY"),
                        normalizePage(page), normalizePageSize(pageSize)),
                "工单列表查询");
    }

    /**
     * 工单执行化。管理员专属。
     *
     * <p>与回滚同一套顺序：权限先于下游判定，非管理员直接 4011，
     * 不产生一次无效的软删/停用调用与审计噪音。</p>
     */
    @Override
    public Object ticketConfirm(Integer ticketId, DroneSession session) {
        LoginUser user = requireAdmin(session, "工单执行");
        return unwrap(governanceClient.ticketConfirm(requireTicketId(ticketId), operatorOf(user)),
                "工单执行");
    }

    @Override
    public Object ticketDismiss(Integer ticketId, String note, DroneSession session) {
        LoginUser user = requireAdmin(session, "工单忽略");
        return unwrap(governanceClient.ticketDismiss(requireTicketId(ticketId), note, operatorOf(user)),
                "工单忽略");
    }

    @Override
    public Object inspectionScan(DroneSession session, boolean dryRun) {
        LoginUser user = requireAdmin(session, "巡检扫描");
        return unwrap(governanceClient.inspectionScan(dryRun, operatorOf(user)), "巡检扫描");
    }

    @Override
    public Object purgeExpired(DroneSession session, boolean dryRun) {
        LoginUser user = requireAdmin(session, "TTL 清理");
        return unwrap(governanceClient.purgeExpired(dryRun, operatorOf(user)), "TTL 清理");
    }

    // ------------------------------------------------------------ 身份与权限

    private LoginUser requireLogin(DroneSession session) {
        LoginUser user = session == null ? null : session.getUser();
        if (user == null || !StringUtils.hasText(user.getUsername())) {
            if (session != null) {
                log.warn("会话存在但用户信息缺失，按未登录处理");
            }
            throw new ServiceException(StatusCode.UNAUTHORIZED);
        }
        return user;
    }

    /**
     * 管理员收口。
     *
     * <p>与"软删只能删自己的"不同：治理视图与对账看的是<b>全库</b>状态
     * （谁上传的、在哪儿、冗余多少），对普通用户既无意义也有信息泄露面，
     * 故一律管理员专属；越权直接 4011，不下发下游。</p>
     */
    private LoginUser requireAdmin(DroneSession session, String action) {
        LoginUser user = requireLogin(session);
        if (!isAdmin(session)) {
            log.warn("越权访问治理接口被拒：action={} user={}", action, user.getUsername());
            throw new ServiceException(StatusCode.PERMISSION_DENIED,
                    "该治理操作仅管理员可用");
        }
        return user;
    }

    private boolean isAdmin(DroneSession session) {
        LoginUser user = session == null ? null : session.getUser();
        return user != null && ROLE_ADMIN.equalsIgnoreCase(user.getPermission());
    }

    // ------------------------------------------------------------ 分页收敛

    private int normalizePage(Integer page) {
        return page == null || page < 1 ? 1 : page;
    }

    private int normalizePageSize(Integer size) {
        if (size == null || size < 1) {
            return DEFAULT_PAGE_SIZE;
        }
        return Math.min(size, MAX_PAGE_SIZE);
    }

    /**
     * 下拉筛选值的归一与白名单校验（空串视为"不筛选"）。
     *
     * <p>早拦一次的价值不只是省一次往返：Python 侧收到非法枚举会返回 3001，
     * 而那会让前端的"参数错误"与"业务规则拒绝"混在同一个提示里——
     * 参数问题在这里说清，用户才知道该改什么。</p>
     */
    private String normalizeEnum(String value, java.util.Set<String> allowed, String message) {
        if (!StringUtils.hasText(value)) {
            return null;
        }
        String normalized = value.trim().toUpperCase();
        if (!allowed.contains(normalized)) {
            throw new ServiceException(StatusCode.VALIDATE_ERROR, message);
        }
        return normalized;
    }

    private int requireTicketId(Integer ticketId) {
        if (ticketId == null || ticketId < 1) {
            throw new ServiceException(StatusCode.VALIDATE_ERROR, "工单 id 必须为正整数");
        }
        return ticketId;
    }

    /** 审计用操作人：取真实登录名，缺失时回落到 manual（审计里要能区分人与系统）。 */
    private String operatorOf(LoginUser user) {
        return StringUtils.hasText(user.getUsername()) ? user.getUsername() : FALLBACK_OPERATOR;
    }

    // ------------------------------------------------------------ 响应拆包

    /**
     * 统一拆包（与 {@code KbServiceImpl} 同口径，含 G2 确认门特判）。
     *
     * <p>顺序很重要：<b>先判确认门，再判普通失败</b>。确认门同样是非 2000，
     * 但它的 data（expectedDocName / hits / maxSimilarity）是前端弹确认框的唯一依据，
     * 走普通失败分支就被丢掉了。</p>
     */
    private Object unwrap(AgentApiResponse<Object> response, String action) {
        if (response == null) {
            log.error("{}失败：响应体为空", action);
            throw new ServiceException(StatusCode.AGENT_UNAVAILABLE);
        }
        if (!response.isSuccess()) {
            if (response.isConfirmRequired()) {
                log.info("{}需要二次确认：{}", action, response.getMessage());
                throw new ConfirmRequiredException(response.getMessage(), response.getData());
            }
            StatusCode mapped = mapCode(response.getCode());
            String message = StringUtils.hasText(response.getMessage())
                    ? response.getMessage() : mapped.getMessage();
            log.warn("{}失败：pythonCode={} → {} message={}",
                    action, response.getCode(), mapped.name(), message);
            throw new ServiceException(mapped, message);
        }
        return response.getData();
    }

    /** Python 业务码 → 本系统状态码（与 KbServiceImpl 同一张映射表）。 */
    private StatusCode mapCode(Integer code) {
        if (code == null) {
            return StatusCode.OPERATION_FAILED;
        }
        return switch (code) {
            case 3001 -> StatusCode.VALIDATE_ERROR;
            case 3002 -> StatusCode.DATA_UNEXISTS;
            case 3010 -> StatusCode.AGENT_UNAVAILABLE;
            case 3011 -> StatusCode.AGENT_CALL_ERROR;
            case 4010 -> StatusCode.UNAUTHORIZED;
            default -> StatusCode.OPERATION_FAILED;
        };
    }
}
