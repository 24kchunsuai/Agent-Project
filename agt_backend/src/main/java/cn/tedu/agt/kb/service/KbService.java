package cn.tedu.agt.kb.service;

import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.kb.pojo.dto.KbUploadParam;

/**
 * 知识库管理服务。
 *
 * <p>本层职责有三：<strong>鉴权收口</strong>（谁能做什么）、
 * <strong>身份注入</strong>（owner 以服务端登录态为准）、
 * <strong>错误翻译</strong>（把 Python 的业务码映射成本系统 StatusCode 并保留原提示）。</p>
 */
public interface KbService {

    /** 上传文档入库。需登录。 */
    Object upload(KbUploadParam param, DroneSession session);

    /** 文档列表（分页）：管理员看全库，普通用户看自己的 + 公开的。 */
    Object documents(DroneSession session, Integer page, Integer size);

    /** 删除文档：移入回收站（可恢复）。需登录；非管理员只能删自己上传的。 */
    Object delete(String docId, DroneSession session);

    /** 回收站列表（分页）。需登录。 */
    Object recycled(DroneSession session, Integer page, Integer size);

    /**
     * 从回收站恢复文档。需登录；非管理员只能恢复自己删除的。
     *
     * <p>G2 起先过 L3 语义查重确认门：未带 confirm 且命中相似语料时，抛
     * {@code ConfirmRequiredException}（带相似文档列表，<b>此时并未恢复</b>），
     * 前端确认后带 confirm=true 重发。</p>
     */
    Object restore(String docId, boolean confirm, DroneSession session);

    /**
     * 彻底删除（不可恢复）。G2 起为管理员专属 + 双确认（confirm + 文档名复述）：
     * 非管理员在 Java 层直接拒绝；确认信息不完整时抛 {@code ConfirmRequiredException}
     * 带回 {@code expectedDocName}，不执行任何删除。
     */
    Object purge(String docId, boolean confirm, String confirmDocName, DroneSession session);

    /** 知识库统计。 */
    Object stats(DroneSession session);

    /** 检索预览：验证刚入库的内容能否被检索到。 */
    Object search(String query, DroneSession session);
}
