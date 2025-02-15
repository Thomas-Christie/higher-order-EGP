import functools
import logging
import torch
from torch_geometric.loader import DataLoader
import torch.optim as optim
from torchvision import transforms
from models.gnn import GNN
from exp import expander_graph_generation

from tqdm import tqdm
import argparse
import numpy as np
import pandas as pd
import os

### importing OGB
from ogb.graphproppred import PygGraphPropPredDataset, Evaluator

### importing utils
from models.utils import ASTNodeEncoder, get_vocab_mapping, str2bool, set_seed
### for data transform
from models.utils import augment_edge, encode_y_to_arr, decode_arr_to_seq

multicls_criterion = torch.nn.CrossEntropyLoss()


def train(model, device, loader, optimizer):
    model.train()

    loss_accum = 0
    for step, batch in enumerate(tqdm(loader, desc="Iteration")):
        batch = batch.to(device)

        if batch.x.shape[0] == 1 or batch.batch[-1] == 0:
            pass
        else:
            pred_list = model(batch)
            optimizer.zero_grad()

            loss = 0
            for i in range(len(pred_list)):
                loss += multicls_criterion(pred_list[i].to(torch.float32), batch.y_arr[:, i])

            loss = loss / len(pred_list)

            loss.backward()
            optimizer.step()

            loss_accum += loss.item()

    logging.info('Average training loss: {}'.format(loss_accum / (step + 1)))


def eval(model, device, loader, evaluator, arr_to_seq):
    model.eval()
    seq_ref_list = []
    seq_pred_list = []

    for step, batch in enumerate(tqdm(loader, desc="Iteration")):
        batch = batch.to(device)

        if batch.x.shape[0] == 1:
            pass
        else:
            with torch.no_grad():
                pred_list = model(batch)

            mat = []
            for i in range(len(pred_list)):
                mat.append(torch.argmax(pred_list[i], dim=1).view(-1, 1))
            mat = torch.cat(mat, dim=1)

            seq_pred = [arr_to_seq(arr) for arr in mat]

            # PyG = 1.4.3
            # seq_ref = [batch.y[i][0] for i in range(len(batch.y))]

            # PyG >= 1.5.0
            seq_ref = [batch.y[i] for i in range(len(batch.y))]

            seq_ref_list.extend(seq_ref)
            seq_pred_list.extend(seq_pred)

    input_dict = {"seq_ref": seq_ref_list, "seq_pred": seq_pred_list}

    return evaluator.eval(input_dict)


def main():
    # Training settings
    parser = argparse.ArgumentParser(description='GNN baselines on ogbg-code2 data with Pytorch Geometrics')
    parser.add_argument('--seed', type=int, default=1,
                        help='random seed for training')
    parser.add_argument('--device', type=int, default=0,
                        help='which gpu to use if any (default: 0)')
    parser.add_argument('--gnn', type=str, default='gin',
                        help='GNN gin or gcn, (default: gin)')
    parser.add_argument('--drop_ratio', type=float, default=0,
                        help='dropout ratio (default: 0)')
    parser.add_argument('--max_seq_len', type=int, default=5,
                        help='maximum sequence length to predict (default: 5)')
    parser.add_argument('--num_vocab', type=int, default=5000,
                        help='the number of vocabulary used for sequence prediction (default: 5000)')
    parser.add_argument('--num_layer', type=int, default=5,
                        help='number of GNN message passing layers (default: 5)')
    parser.add_argument('--emb_dim', type=int, default=300,
                        help='dimensionality of hidden units in GNNs (default: 300)')
    parser.add_argument('--batch_size', type=int, default=128,
                        help='input batch size for training (default: 128)')
    parser.add_argument('--epochs', type=int, default=25,
                        help='number of epochs to train (default: 25)')
    parser.add_argument('--random_split', dest='random_split', type=str2bool, default=False)
    parser.add_argument('--num_workers', type=int, default=0,
                        help='number of workers (default: 0)')
    parser.add_argument('--dataset', type=str, default="ogbg-code2",
                        choices = ["ogbg-code2"],
                        help='dataset name (default: ogbg-code2)')
    parser.add_argument('--expander', dest='expander', type=str2bool, default=True,
                        help='whether to use expander graph propagation')
    parser.add_argument('--expander_graph_generation_method', type=str, default="perfect-matchings",
                        choices=['perfect-matchings', 'ramanujan-bipartite',
                                 'perfect-matchings-shortest-path',
                                 'perfect-matchings-access-time'],
                        help='method for generating expander graph')
    parser.add_argument('--expander_graph_order', type=int, default=3,
                        help='order of hypergraph expander graph')
    # parser.add_argument('--random_seed', type=int, default=42,
    #                     help='random seed used when generating ramanujan bipartite graphs')
    parser.add_argument('--expander_edge_handling', type=str, default='masking',
                        choices=['masking', 'learn-features', 'summation', 'summation-mlp'],
                        help='method to handle expander edge nodes')
    # parser.add_argument('--save_dir', type=str, default="",
    #                      help='save_dir to output result (default: )')
    args = parser.parse_args()

    # Set the seed for everything
    set_seed(args.seed)

    expander_graph_generation_fn = None
    if args.expander_graph_generation_method == "perfect-matchings":
        expander_graph_generation_fn = functools.partial(
            expander_graph_generation.add_expander_edges_via_perfect_matchings,
            args.expander_graph_order,
            "code2")
    elif args.expander_graph_generation_method == "perfect-matchings-shortest-path":
        expander_graph_generation_fn = functools.partial(
            expander_graph_generation.add_expander_edges_via_perfect_matchings_shortest_paths_heuristics,
            args.expander_graph_order,
            "code2")
    elif args.expander_graph_generation_method == "perfect-matchings-access-time":
        expander_graph_generation_fn = functools.partial(
            expander_graph_generation.add_expander_edges_via_perfect_matchings_access_time_heuristics,
            args.expander_graph_order,
            "code2")
    elif args.expander_graph_generation_method == "ramanujan-bipartite":
        expander_graph_generation_fn = functools.partial(
            expander_graph_generation.add_expander_edges_via_ramanujan_bipartite_graph,
            args.expander_graph_order,
            "code2")

    device = torch.device("cuda:" + str(args.device)) if torch.cuda.is_available() else torch.device("cpu")

    # Set path
    path = os.path.join(os.getcwd() + f"/logs/{args.dataset}/")
    if not os.path.exists(path):
        os.makedirs(path)
    save_dir = os.path.join(path, f"{args.expander_graph_generation_method}_seed{args.seed}_")
    logging.basicConfig(level=logging.INFO,
                        handlers=[
                            logging.FileHandler(save_dir + "log.txt"),
                            logging.StreamHandler()
                        ])
    logging.info(args)
    logging.info(f'Using: {device}')
    logging.info(f"Using seed {args.seed}")
    logging.info(f"Dataset: {args.dataset}")
    logging.info(f"Expander generation method: {args.expander_graph_generation_method}")
    logging.info(f"Expander graph order: {args.expander_graph_order}")
    logging.info(f"Expander edge handling: {args.expander_edge_handling}")

    ### automatic dataloading and splitting
    if not args.expander:
        dataset = PygGraphPropPredDataset(name=args.dataset)
    else:
        dataset = PygGraphPropPredDataset(name=args.dataset, pre_transform=expander_graph_generation_fn)

    seq_len_list = np.array([len(seq) for seq in dataset.data.y])
    logging.info('Target seqence less or equal to {} is {}%.'.format(args.max_seq_len,
                                                              np.sum(seq_len_list <= args.max_seq_len) / len(
                                                                  seq_len_list)))

    split_idx = dataset.get_idx_split()

    if args.random_split:
        logging.info('Using random split')
        perm = torch.randperm(len(dataset))
        num_train, num_valid, num_test = len(split_idx['train']), len(split_idx['valid']), len(split_idx['test'])
        split_idx['train'] = perm[:num_train]
        split_idx['valid'] = perm[num_train:num_train + num_valid]
        split_idx['test'] = perm[num_train + num_valid:]

        assert (len(split_idx['train']) == num_train)
        assert (len(split_idx['valid']) == num_valid)
        assert (len(split_idx['test']) == num_test)

    # logging.info(split_idx['train'])
    # logging.info(split_idx['valid'])
    # logging.info(split_idx['test'])

    # train_method_name = [' '.join(dataset.data.y[i]) for i in split_idx['train']]
    # valid_method_name = [' '.join(dataset.data.y[i]) for i in split_idx['valid']]
    # test_method_name = [' '.join(dataset.data.y[i]) for i in split_idx['test']]
    # logging.info('#train')
    # logging.info(len(train_method_name))
    # logging.info('#valid')
    # logging.info(len(valid_method_name))
    # logging.info('#test')
    # logging.info(len(test_method_name))

    # train_method_name_set = set(train_method_name)
    # valid_method_name_set = set(valid_method_name)
    # test_method_name_set = set(test_method_name)

    # # unique method name
    # logging.info('#unique train')
    # logging.info(len(train_method_name_set))
    # logging.info('#unique valid')
    # logging.info(len(valid_method_name_set))
    # logging.info('#unique test')
    # logging.info(len(test_method_name_set))

    # # unique valid/test method name
    # logging.info('#valid unseen during training')
    # logging.info(len(valid_method_name_set - train_method_name_set))
    # logging.info('#test unseen during training')
    # logging.info(len(test_method_name_set - train_method_name_set))

    ### building vocabulary for sequence predition. Only use training data.

    vocab2idx, idx2vocab = get_vocab_mapping([dataset.data.y[i] for i in split_idx['train']], args.num_vocab)

    # test encoder and decoder
    # for data in dataset:
    #     # PyG >= 1.5.0
    #     logging.info(data.y)
    #
    #     # PyG 1.4.3
    #     # logging.info(data.y[0])
    #     data = encode_y_to_arr(data, vocab2idx, args.max_seq_len)
    #     logging.info(data.y_arr[0])
    #     decoded_seq = decode_arr_to_seq(data.y_arr[0], idx2vocab)
    #     logging.info(decoded_seq)
    #     logging.info('')

    ## test augment_edge
    # data = dataset[2]
    # logging.info(data)
    # data_augmented = augment_edge(data)
    # logging.info(data_augmented)

    ### set the transform function
    # augment_edge: add next-token edge as well as inverse edges. add edge attributes.
    # encode_y_to_arr: add y_arr to PyG data object, indicating the array representation of a sequence.
    dataset.transform = transforms.Compose(
        [augment_edge, lambda data: encode_y_to_arr(data, vocab2idx, args.max_seq_len)])

    ### automatic evaluator. takes dataset name as input
    evaluator = Evaluator(args.dataset)

    train_loader = DataLoader(dataset[split_idx["train"]], batch_size=args.batch_size, shuffle=True,
                              num_workers=args.num_workers)
    valid_loader = DataLoader(dataset[split_idx["valid"]], batch_size=args.batch_size, shuffle=False,
                              num_workers=args.num_workers)
    test_loader = DataLoader(dataset[split_idx["test"]], batch_size=args.batch_size, shuffle=False,
                             num_workers=args.num_workers)

    nodetypes_mapping = pd.read_csv(os.path.join(dataset.root, 'mapping', 'typeidx2type.csv.gz'))
    nodeattributes_mapping = pd.read_csv(os.path.join(dataset.root, 'mapping', 'attridx2attr.csv.gz'))

    logging.info(nodeattributes_mapping)

    ### Encoding node features into emb_dim vectors.
    ### The following three node features are used.
    # 1. node type
    # 2. node attribute
    # 3. node depth
    node_encoder = ASTNodeEncoder(args.emb_dim, num_nodetypes=len(nodetypes_mapping['type']),
                                  num_nodeattributes=len(nodeattributes_mapping['attr']), max_depth=20)

    if args.gnn == 'gin':
        model = GNN(task="code2", num_class=len(vocab2idx), max_seq_len=args.max_seq_len, node_encoder=node_encoder,
                    num_layer=args.num_layer, gnn_type='gin', emb_dim=args.emb_dim, drop_ratio=args.drop_ratio,
                    expander=args.expander, expander_edge_handling=args.expander_edge_handling).to(device)
    elif args.gnn == 'gcn':
        model = GNN(task="code2", num_class=len(vocab2idx), max_seq_len=args.max_seq_len, node_encoder=node_encoder,
                    num_layer=args.num_layer, gnn_type='gcn', emb_dim=args.emb_dim, drop_ratio=args.drop_ratio,
                    expander=args.expander, expander_edge_handling=args.expander_edge_handling).to(device)
    else:
        raise ValueError('Invalid GNN type')

    optimizer = optim.Adam(model.parameters(), lr=0.001)

    logging.info(f'#Params: {sum(p.numel() for p in model.parameters())}')

    valid_curve = []
    test_curve = []
    train_curve = []

    best_val_so_far = 0
    for epoch in range(1, args.epochs + 1):
        logging.info("=====Epoch {}".format(epoch))
        logging.info('Training...')
        train(model, device, train_loader, optimizer)

        logging.info('Evaluating...')
        train_perf = eval(model, device, train_loader, evaluator,
                          arr_to_seq=lambda arr: decode_arr_to_seq(arr, idx2vocab))
        valid_perf = eval(model, device, valid_loader, evaluator,
                          arr_to_seq=lambda arr: decode_arr_to_seq(arr, idx2vocab))
        test_perf = eval(model, device, test_loader, evaluator,
                         arr_to_seq=lambda arr: decode_arr_to_seq(arr, idx2vocab))

        logging.info({'Train': train_perf, 'Validation': valid_perf, 'Test': test_perf})

        train_curve.append(train_perf[dataset.eval_metric])
        valid_curve.append(valid_perf[dataset.eval_metric])
        test_curve.append(test_perf[dataset.eval_metric])
        torch.save({'Val': valid_curve, 'Test': test_curve, 'Train': train_curve}, save_dir + "_curves")
        if valid_perf[dataset.eval_metric] > best_val_so_far:
            torch.save(model.state_dict(), save_dir + "best_val_model.pt")
            best_val_so_far = valid_perf[dataset.eval_metric]

    logging.info('F1')
    best_val_epoch = np.argmax(np.array(valid_curve))
    best_train = max(train_curve)
    logging.info('Finished training!')
    logging.info('Best validation score: {}'.format(valid_curve[best_val_epoch]))
    logging.info('Test score: {}'.format(test_curve[best_val_epoch]))

    if not save_dir == '':
        torch.save({'Val': valid_curve[best_val_epoch], 'Test': test_curve[best_val_epoch],
                    'Train': train_curve[best_val_epoch], 'BestTrain': best_train}, save_dir + "_best")
        torch.save({'Val': valid_curve, 'Test': test_curve, 'Train': train_curve}, save_dir + "_curves")
        torch.save(model.state_dict(), save_dir + "final_model.pt")


if __name__ == "__main__":
    main()