package cn.tedu.agt.kb.service.impl;

import cn.tedu.agt.agent.pojo.AgentApiResponse;
import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.auth.LoginUser;
import cn.tedu.agt.base.exception.ConfirmRequiredException;
import cn.tedu.agt.base.exception.ServiceException;
import cn.tedu.agt.base.response.StatusCode;
import cn.tedu.agt.kb.client.KbClient;
import cn.tedu.agt.kb.pojo.dto.KbUploadParam;
import cn.tedu.agt.kb.service.KbService;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

@Slf4j
@Service
public class KbServiceImpl implements KbService {

    /** 管理员角色标识，与 jsdstu-3 的 RoleEnum 一致 */
    private static final String ROLE_ADMIN = "admin";

    private static final String VISIBILITY_PUBLIC = "public";
    private static final String VISIBILITY_PRIVATE = "private";

    /** 可见范围：只看自己和公开内容 */
    private static final String SCOPE_MINE = "mine";
    /** 可见范围：全库（管理员） */
    private static final String SCOPE_ALL = "all";

    /** 分页默认值与上限，与 Python 端 kb_service 的常量保持一致 */
    private static final int DEFAULT_PAGE_SIZE = 20;
    private static final int MAX_PAGE_SIZE = 100;

    @Autowired
    private KbClient kbClient;

    // ------------------------------------------------------------ 对外能力

    @Override
    public Object upload(KbUploadParam param, DroneSession session) {
        LoginUser user = requireLogin(session);

        // 归属以服务端登录态为准：前端传的 owner 一律忽略
        param.setOwner(user.getUsername());
        // 可见性白名单校验，防止写入 retriever 鉴权表达式不认识的取值
        param.setVisibility(normalizeVisibility(param.getVisibility()));

        return unwrap(kbClient.upload(param), "知识库入库");
    }

    @Override
    public Object documents(DroneSession session, Integer page, Integer size) {
        int p = normalizePage(page);
        int s = normalizePageSize(size);
        return unwrap(kbClient.documents(ownerOf(session), scopeOf(session), p, s), "文档列表查询");
    }

    @Override
    public Object delete(String docId, DroneSession session) {
        LoginUser user = requireLogin(session);
        if (!StringUtils.hasText(docId)) {
            throw new ServiceException(StatusCode.VALIDATE_ERROR, "文档标识不能为空");
        }
        return unwrap(
                kbClient.delete(docId, user.getUsername(), isAdmin(session)),
                "文档删除");
    }

    @Override
    public Object recycled(DroneSession session, Integer page, Integer size) {
        requireLogin(session);
        int p = normalizePage(page);
        int s = normalizePageSize(size);
        return unwrap(kbClient.recycle(ownerOf(session), scopeOf(session), p, s), "回收站列表查询");
    }

    @Override
    public Object restore(String docId, boolean confirm, DroneSession session) {
        LoginUser user = requireLogin(session);
        if (!StringUtils.hasText(docId)) {
            throw new ServiceException(StatusCode.VALIDATE_ERROR, "文档标识不能为空");
        }
        // confirm 原样透传：L3 确认门由 Python 判定（阈值与相似语料都在那边），Java 只做传递
        return unwrap(
                kbClient.restore(docId, user.getUsername(), isAdmin(session), confirm),
                "文档恢复");
    }

    @Override
    public Object purge(String docId, boolean confirm, String confirmDocName, DroneSession session) {
        LoginUser user = requireLogin(session);
        if (!StringUtils.hasText(docId)) {
            throw new ServiceException(StatusCode.VALIDATE_ERROR, "文档标识不能为空");
        }
        // G2 铁律 2：彻底删除只允许管理员。在 Java 层先拦（规约 §6.2），
        // 非管理员连"确认门"都不必走到——越权请求不给任何文档名信息。
        if (!isAdmin(session)) {
            throw new ServiceException(StatusCode.PERMISSION_DENIED,
                    "彻底删除为管理员操作，请联系管理员处理");
        }
        // 已在此处校验过权限，故给下游固定 is_admin=true
        return unwrap(
                kbClient.purge(docId, user.getUsername(), true, confirm, confirmDocName),
                "文档彻底删除");
    }

    @Override
    public Object stats(DroneSession session) {
        return unwrap(kbClient.stats(ownerOf(session), scopeOf(session)), "知识库统计");
    }

    @Override
    public Object search(String query, DroneSession session) {
        if (!StringUtils.hasText(query)) {
            throw new ServiceException(StatusCode.VALIDATE_ERROR, "检索内容不能为空");
        }
        // 传入当前用户名：检索时可见 private 文档；匿名只可见 public
        return unwrap(kbClient.search(query, ownerOf(session)), "知识库检索");
    }

    // ------------------------------------------------------------ 分页收敛

    /** 页码收敛：null/非正数回到第 1 页，防止构造负页码打穿下游。 */
    private int normalizePage(Integer page) {
        return page == null || page < 1 ? 1 : page;
    }

    /** 每页条数收敛：null 用默认 20；越界夹回 [1, 100]，与 Python 端口径一致。 */
    private int normalizePageSize(Integer size) {
        if (size == null || size < 1) {
            return DEFAULT_PAGE_SIZE;
        }
        return Math.min(size, MAX_PAGE_SIZE);
    }

    // ------------------------------------------------------------ 身份与权限

    /** 必须登录的接口收口点：匿名一律 4010，由前端统一引导登录。 */
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

    /** 当前用户名；匿名返回 null（列表/统计允许匿名，仅可见 public 内容）。 */
    private String ownerOf(DroneSession session) {
        LoginUser user = session == null ? null : session.getUser();
        return user == null ? null : user.getUsername();
    }

    private boolean isAdmin(DroneSession session) {
        LoginUser user = session == null ? null : session.getUser();
        return user != null && ROLE_ADMIN.equalsIgnoreCase(user.getPermission());
    }

    /** 管理员看全库，其余人只看自己的 + 公开的。 */
    private String scopeOf(DroneSession session) {
        return isAdmin(session) ? SCOPE_ALL : SCOPE_MINE;
    }

    private String normalizeVisibility(String visibility) {
        if (VISIBILITY_PRIVATE.equalsIgnoreCase(visibility)) {
            return VISIBILITY_PRIVATE;
        }
        if (StringUtils.hasText(visibility) && !VISIBILITY_PUBLIC.equalsIgnoreCase(visibility)) {
            log.warn("未知的可见性取值，按 public 处理：{}", visibility);
        }
        return VISIBILITY_PUBLIC;
    }

    // ------------------------------------------------------------ 响应拆包

    /**
     * 统一拆包。
     *
     * <p>与对话链路的 {@code unwrap} 有一处关键区别：<strong>保留 Python 的原始提示文案</strong>。
     * 知识库的失败多半是用户可直接修正的问题（格式不支持、内容为空、文件过大、
     * 只能删自己的文档），若笼统吞成"服务不可用"，用户就无从下手。</p>
     *
     * <p>G2 新增第二种区别：<strong>确认门要带 data 出去</strong>。判定顺序不能颠倒——
     * 确认门也是非 2000，若先走普通失败分支，{@code expectedDocName / hits} 就被丢了。</p>
     */
    private Object unwrap(AgentApiResponse<Object> response, String action) {
        if (response == null) {
            log.error("{}失败：响应体为空", action);
            throw new ServiceException(StatusCode.AGENT_UNAVAILABLE);
        }
        if (!response.isSuccess()) {
            // 确认门先判（G2）：它同样是非 2000，但 data 是前端弹确认框的依据，
            // 绝不能按普通失败处理（那样 data 会被丢掉，用户只能盲点确认）
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

    /** Python 业务码 → 本系统状态码，保证前端只需认一套编码段。 */
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
