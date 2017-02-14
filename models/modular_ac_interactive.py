from misc import util
import net
from reflex_meta import ReflexMetaModel

from collections import namedtuple, defaultdict
import logging
import numpy as np
import os
import tensorflow as tf
from tensorflow.python.framework.ops import IndexedSlicesValue

N_UPDATE = 2000
N_BATCH = 2000

N_HIDDEN = 128
N_EMBED = 64

DISCOUNT = 0.9

ActorModule = namedtuple("ActorModule", ["t_probs", "t_chosen_prob", "params",
        "t_decrement_op"])
CriticModule = namedtuple("CriticModule", ["t_value", "params"])
Trainer = namedtuple("Trainer", ["t_loss", "t_grad", "t_train_op"])
InputBundle = namedtuple("InputBundle", ["t_arg", "t_step", "t_feats", 
        "t_action_mask", "t_reward"])

ModelState = namedtuple("ModelState", ["action", "arg", "remaining", "task", "step"])

def increment_sparse_or_dense(into, increment):
    assert isinstance(into, np.ndarray)
    if isinstance(increment, IndexedSlicesValue):
        for i in range(increment.values.shape[0]):
            into[increment.indices[i], :] += increment.values[i, :]
    else:
        into += increment

class ModularACInteractiveModel(object):
    def __init__(self, config):
        self.experiences = []
        self.world = None
        tf.set_random_seed(0)
        self.next_actor_seed = 0
        self.config = config

    def prepare(self, world, trainer):
        assert self.world is None
        self.world = world
        self.trainer = trainer

        self.n_tasks = len(trainer.task_index)
        self.n_modules = len(trainer.subtask_index)

        #self.meta = meta
        #self.meta = ReflexMetaModel(world, trainer.subtask_index,
        #        trainer.cookbook.index)
        self.metas = []
        for i_task in range(self.n_tasks):
            self.metas.append(ReflexMetaModel(world, trainer.subtask_index,
                trainer.cookbook.index))

        self.n_actions = world.n_actions + 1
        self.t_n_steps = tf.Variable(1., name="n_steps")
        self.t_inc_steps = self.t_n_steps.assign(self.t_n_steps + 1)
        # TODO configurable optimizer
        self.optimizer = tf.train.RMSPropOptimizer(0.001)


        def build_actor(index, t_input, t_action_mask, extra_params=[]):
            with tf.variable_scope("actor_%s" % index):
                t_action_score, v_action = net.mlp(t_input, (N_HIDDEN, self.n_actions))

                # TODO this is pretty gross
                v_bias = v_action[-1]
                assert "b1" in v_bias.name
                t_decrement_op = v_bias[-1].assign(v_bias[-1] - 3)

                t_action_logprobs = tf.nn.log_softmax(t_action_score)
                t_chosen_prob = tf.reduce_sum(t_action_mask * t_action_logprobs, 
                        reduction_indices=(1,))

            return ActorModule(t_action_logprobs, t_chosen_prob, 
                    v_action+extra_params, t_decrement_op)

        def build_critic(index, t_input, t_reward, extra_params=[]):
            with tf.variable_scope("critic_%s" % index):
                if self.config.model.baseline in ("task", "common"):
                    t_value = tf.get_variable("b", shape=(),
                            initializer=tf.constant_initializer(0.0))
                    v_value = [t_value]
                elif self.config.model.baseline == "state":
                    t_value, v_value = net.mlp(t_input, (1,))
                    t_value = tf.squeeze(t_value)
                else:
                    raise NotImplementedError(
                            "Baseline %s is not implemented" % self.config.model.baseline)
            return CriticModule(t_value, v_value + extra_params)

        def build_actor_trainer(actor, critic, t_reward):
            t_advantage = t_reward - critic.t_value
            # TODO configurable entropy regularizer
            actor_loss = -tf.reduce_sum(actor.t_chosen_prob * t_advantage) + \
                    0.001 * tf.reduce_sum(tf.exp(actor.t_probs) * actor.t_probs)
            actor_grad = tf.gradients(actor_loss, actor.params)
            actor_trainer = Trainer(actor_loss, actor_grad, 
                    self.optimizer.minimize(actor_loss, var_list=actor.params))
            return actor_trainer

        def build_critic_trainer(t_reward, critic):
            t_advantage = t_reward - critic.t_value
            critic_loss = tf.reduce_sum(tf.square(t_advantage))
            critic_grad = tf.gradients(critic_loss, critic.params)
            critic_trainer = Trainer(critic_loss, critic_grad,
                    self.optimizer.minimize(critic_loss, var_list=critic.params))
            return critic_trainer

        # placeholders
        t_arg = tf.placeholder(tf.int32, shape=(None,))
        t_step = tf.placeholder(tf.float32, shape=(None, 1))
        t_feats = tf.placeholder(tf.float32, shape=(None, world.n_features))
        t_action_mask = tf.placeholder(tf.float32, shape=(None, self.n_actions))
        t_reward = tf.placeholder(tf.float32, shape=(None,))

        if self.config.model.use_args:
            t_embed, v_embed = net.embed(t_arg, len(trainer.cookbook.index),
                    N_EMBED)
            xp = v_embed
            t_input = tf.concat(1, (t_embed, t_feats))
        else:
            t_input = t_feats
            xp = []

        actors = {}
        actor_trainers = {}
        critics = {}
        critic_trainers = {}

        for i_module in range(self.n_modules):
            actor = build_actor(i_module, t_input, t_action_mask, extra_params=xp)
            actors[i_module] = actor

        if self.config.model.baseline == "common":
            common_critic = build_critic(0, t_input, t_reward, extra_params=xp)
        for i_task in range(self.n_tasks):
            if self.config.model.baseline == "common":
                critic = common_critic
            else:
                critic = build_critic(i_task, t_input, t_reward, extra_params=xp)
            for i_module in range(self.n_modules):
                critics[i_task, i_module] = critic

        for i_module in range(self.n_modules):
            for i_task in range(self.n_tasks):
                critic = critics[i_task, i_module]
                critic_trainer = build_critic_trainer(t_reward, critic)
                critic_trainers[i_task, i_module] = critic_trainer

                actor = actors[i_module]
                actor_trainer = build_actor_trainer(actor, critic, t_reward)
                actor_trainers[i_task, i_module] = actor_trainer

        self.t_gradient_placeholders = {}
        self.t_update_gradient_op = None

        params = []
        for module in actors.values() + critics.values():
            params += module.params
        self.saver = tf.train.Saver()

        self.session = tf.Session()
        self.session.run(tf.initialize_all_variables())
        self.session.run([actor.t_decrement_op for actor in actors.values()])

        self.actors = actors
        self.critics = critics
        self.actor_trainers = actor_trainers
        self.critic_trainers = critic_trainers
        self.inputs = InputBundle(t_arg, t_step, t_feats, t_action_mask, t_reward)

        #self.saver.restore(self.session, "experiments/craft_holdout/modular_ac.chk")

    def init(self, states, tasks):
        n_act_batch = len(states)

        #self.subtask, self.arg = zip(*self.meta.act(states, init=True))
        #self.subtask = list(self.subtask)

        self.subtask = []
        self.arg = []

        self.arg = list(self.arg)
        self.i_task = []
        for i in range(n_act_batch):
            i_task = self.trainer.task_index[tasks[i]]
            self.i_task.append(i_task)
            (subtask, arg), = self.metas[i_task].act([states[i]], init=True)
            self.subtask.append(subtask)
            self.arg.append(arg)

        self.i_step = np.zeros((n_act_batch, 1))

        self.randoms = []
        for _ in range(n_act_batch):
            self.randoms.append(np.random.RandomState(self.next_actor_seed))
            self.next_actor_seed += 1

    def save(self):
        self.saver.save(self.session, 
                os.path.join(self.config.experiment_dir, "modular_ac.chk"))

    def load(self):
        path = os.path.join(self.config.experiment_dir, "modular_ac.chk")
        logging.info("loaded %s", path)
        self.saver.restore(self.session, path)

    def experience(self, episode):
        running_reward = 0
        for transition in episode[::-1]:
            running_reward = running_reward * DISCOUNT + transition.r
            n_transition = transition._replace(r=running_reward)
            if n_transition.a < self.n_actions:
                self.experiences.append(n_transition)
        i_task = episode[0].m1.task
        self.metas[i_task].experience(episode)

    def act(self, states):
        #n_subtasks, n_args = zip(*self.meta.act(states))
        n_subtasks = []
        n_args = []
        for i, state in enumerate(states):
            ((subtask, arg),) = self.metas[self.i_task[i]].act([state])
            n_subtasks.append(subtask)
            n_args.append(arg)

        mstates = self.get_state()
        self.i_step += 1
        by_mod = defaultdict(list)
        n_act_batch = len(self.subtask)

        for i in range(n_act_batch):
            by_mod[self.i_task[i], self.subtask[i]].append(i)

        action = [None] * n_act_batch
        terminate = [None] * n_act_batch

        for k, indices in by_mod.items():
            i_task, i_subtask = k
            if i_subtask == 0:
                continue
            actor = self.actors[i_subtask]
            feed_dict = {
                self.inputs.t_feats: [states[i].features() for i in indices],
            }
            if self.config.model.use_args:
                feed_dict[self.inputs.t_arg] = [mstates[i].arg for i in indices]

            logprobs = self.session.run([actor.t_probs], feed_dict=feed_dict)[0]
            probs = np.exp(logprobs)
            for pr, i in zip(probs, indices):

                if self.i_step[i] >= self.config.model.max_subtask_timesteps:
                    a = self.n_actions
                else:
                    a = self.randoms[i].choice(self.n_actions, p=pr)
                terminate[i] = (n_subtasks[i] == 0)

                if a >= self.world.n_actions:
                    self.i_step[i] = 0.
                    self.subtask[i] = n_subtasks[i]
                    self.arg[i] = n_args[i]
                    #self.meta.counters[i] += 1
                terminate[i] = (self.subtask[i] == 0)
                action[i] = self.world.n_actions if terminate[i] else a

        return action, terminate

    def get_state(self):
        out = []
        for i in range(len(self.subtask)):
            out.append(ModelState(
                self.subtask[i],
                self.arg[i],
                0,
                self.i_task[i],
                0))
        return out

    def train(self, action=None, update_actor=True, update_critic=True):
        #meta_err = self.meta.train()
        meta_err = np.mean([m.train() for m in self.metas])

        if action is None:
            experiences = self.experiences
        else:
            experiences = [e for e in self.experiences if e.m1.action == action]
        if len(experiences) < N_UPDATE:
            return None
        batch = experiences[:N_UPDATE]

        by_mod = defaultdict(list)
        for e in batch:
            by_mod[e.m1.task, e.m1.action].append(e)

        grads = {}
        params = {}
        for module in self.actors.values() + self.critics.values():
            for param in module.params:
                if param.name not in grads:
                    grads[param.name] = np.zeros(param.get_shape(), np.float32)
                    params[param.name] = param
        touched = set()

        total_actor_err = 0
        total_critic_err = 0
        for i_task, i_mod1 in by_mod:
            actor = self.actors[i_mod1]
            critic = self.critics[i_task, i_mod1]
            actor_trainer = self.actor_trainers[i_task, i_mod1]
            critic_trainer = self.critic_trainers[i_task, i_mod1]

            all_exps = by_mod[i_task, i_mod1]
            for i_batch in range(int(np.ceil(1. * len(all_exps) / N_BATCH))):
                exps = all_exps[i_batch * N_BATCH : (i_batch + 1) * N_BATCH]
                s1, m1, a, s2, m2, r = zip(*exps)
                feats1 = [s.features() for s in s1]
                args1 = [m.arg for m in m1]
                steps1 = [m.step for m in m1]
                a_mask = np.zeros((len(exps), self.n_actions))
                for i_datum, aa in enumerate(a):
                    a_mask[i_datum, aa] = 1

                feed_dict = {
                    self.inputs.t_feats: feats1,
                    self.inputs.t_action_mask: a_mask,
                    self.inputs.t_reward: r
                }
                if self.config.model.use_args:
                    feed_dict[self.inputs.t_arg] = args1

                actor_grad, actor_err = self.session.run([actor_trainer.t_grad, actor_trainer.t_loss],
                        feed_dict=feed_dict)
                critic_grad, critic_err = self.session.run([critic_trainer.t_grad, critic_trainer.t_loss], 
                        feed_dict=feed_dict)

                total_actor_err += actor_err
                total_critic_err += critic_err

                if update_actor:
                    for param, grad in zip(actor.params, actor_grad):
                        increment_sparse_or_dense(grads[param.name], grad)
                        touched.add(param.name)
                if update_critic:
                    for param, grad in zip(critic.params, critic_grad):
                        increment_sparse_or_dense(grads[param.name], grad)
                        touched.add(param.name)

        global_norm = 0
        for k in params:
            grads[k] /= N_UPDATE
            global_norm += (grads[k] ** 2).sum()
        rescale = min(1., 1. / global_norm)

        # TODO precompute this part of the graph
        updates = []
        feed_dict = {}
        for k in params:
            param = params[k]
            grad = grads[k]
            grad *= rescale
            if k not in self.t_gradient_placeholders:
                self.t_gradient_placeholders[k] = tf.placeholder(tf.float32, grad.shape)
            feed_dict[self.t_gradient_placeholders[k]] = grad
            updates.append((self.t_gradient_placeholders[k], param))
        if self.t_update_gradient_op is None:
            self.t_update_gradient_op = self.optimizer.apply_gradients(updates)
        self.session.run(self.t_update_gradient_op, feed_dict=feed_dict)

        self.experiences = []
        self.session.run(self.t_inc_steps)

        #return np.asarray([total_actor_err, total_critic_err]) / N_UPDATE

        return np.asarray([total_actor_err, total_critic_err, meta_err * N_UPDATE]) / N_UPDATE
