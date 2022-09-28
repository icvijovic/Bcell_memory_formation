import numpy as np
import pandas as pd

import sys
import argparse

from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

import random
import itertools

from Levenshtein import distance
from copy import deepcopy

#####################################################    

parser = argparse.ArgumentParser(description='Calls cells based on VDJC sequence data')
parser.add_argument('input', 
                    help='VDJC sequence file in tsv format')
parser.add_argument('-cb_umi_seqid_map', 
                    required=True, 
                    nargs='+',
                    help='path to map containing whitelisted cell barcodes, and associated umis and cell reads')
parser.add_argument('-outdir', 
                    default='.', 
                    help="output directory (default: working directory)")
parser.add_argument('-samplesheet', 
                    required=True)
parser.add_argument('-max_VDJ_error', 
                    default=10, 
                    help="max error in vdj part of sequence")
parser.add_argument('-min_droplet_fraction', default=0.1, type=float, 
                    help="minimal fraction of UMIs within droplet to call a cell")
parser.add_argument('-outname', default='.',
                    help="name of output file. Default uses input file prefix up to first underscore.")

args = parser.parse_args()
VDJC_SEQUENCE_TSV = args.input
CB_UMI_SEQ_ID_MAP_FILEs = args.cb_umi_seqid_map
SAMPLESHEET = args.samplesheet
MAX_ERROR_IN_VDJ = args.max_VDJ_error
OUTDIR = args.outdir
OUTNAME=args.outname
MIN_DROPLET_FRACTION = args.min_droplet_fraction


if OUTNAME == ".":
    OUTNAME=VDJC_SEQUENCE_TSV.split("/")[-1].split("_")[0]

#####################################################

df = pd.read_table(VDJC_SEQUENCE_TSV, 
                    usecols=['sequence_id', 'sample_id', 'vdj_sequence', 'c_call', 
                             'c_mismatch', 'c_match_length', 'sequence'])

#####################

samplesheet = pd.read_table(SAMPLESHEET, sep='    ', engine='python')
samplesheet['sample_uid'] = samplesheet.library_uid.astype(str) + "-" + samplesheet.sample_index
samplesheet = samplesheet.set_index('sample_uid')

#####################

seq_id_df = pd.DataFrame({'SEQ_ID':[], 'SEQ_ID_INFO':[]})

for filename in CB_UMI_SEQ_ID_MAP_FILEs:
    seq_id_info = pd.read_table(filename)
    sample_uid = filename.split("/")[-1].split("_")[0]
    seq_id_info['sample_id'] = samplesheet.loc[sample_uid,'sample_id']

    seq_id_df = seq_id_df.append(seq_id_info)
df['SEQ_ID'] = df.sequence_id.str.split("|").map(lambda x: int(x[0].split("=")[-1]))

#####################

sys.stderr.write("INFO: subsetting to reads with valid cell barcodes...\n")

df = df.merge(seq_id_df, on = ['SEQ_ID', 'sample_id'], how='inner')

df.SEQ_ID = df.sample_id + "_" + df.SEQ_ID.astype(str)

df['cb_umi_s'] = df.SEQ_ID_INFO.map(lambda x: [y for y in x.split("|")[0].split("=")[-1].split(",")])
df['reads'] = df.SEQ_ID_INFO.map(lambda x: [y for y in x.split("|")[-1].split("=")[-1].split(",")])

df['cb_umi_read_list'] = df.apply(lambda x: [x.cb_umi_s[it] + "_" + x.reads[it] 
                                                for it in range(len(x.cb_umi_s))],
                                         axis=1)
df = df[['sample_id', 'cb_umi_read_list', 'vdj_sequence', 'c_call']]

df = df.explode('cb_umi_read_list')
df['cb'] = df['cb_umi_read_list'].map(lambda x : x.split("_")[0])
df['umi'] = df['cb_umi_read_list'].map(lambda x : x.split("_")[1])
df['reads'] = df['cb_umi_read_list'].map(lambda x : int(x.split("_")[2]))

sys.stderr.write('INFO: aggregating reads belonging to same cell barcode...\n')

cb_df = df.groupby(['sample_id','cb']).agg(list)[['vdj_sequence', 'c_call','umi','reads']]
cb_df = cb_df.reset_index()

cb_df['total_reads'] = cb_df.reads.map(lambda x: sum(x))
cb_df['reads'] = cb_df.reads.apply(lambda x: np.asarray(x, dtype=int))


print('INFO: Dropping barcodes with fewer than 2 reads', file=sys.stderr)

# cell barcode has to be associated with at least 2 reads
cb_df = cb_df[cb_df.total_reads > 1]

print('INFO: total number of valid cell barcodes with at least 2 reads:', file=sys.stderr)
print(cb_df.groupby('sample_id').size(), file=sys.stderr)

cb_df = cb_df.sort_values(by='total_reads', ascending=False)


########################################################

is_cell_dict = {}
is_multiplet_dict = {}
vdj_call_dict = {}
corrected_vdj_dict = {}

components = []
count = 0

for it, row in cb_df.iterrows():

    unique_vdj_seqs = np.unique(np.asarray(row.vdj_sequence))
    umi_support = sorted([(row.vdj_sequence.count(x),x) for x in unique_vdj_seqs])

    umis_per_seq = np.asarray([x for x, y in umi_support])
    unique_vdj_seqs = [y for x, y in umi_support]

    max_umi_support = umi_support[-1][0]
    n = len(unique_vdj_seqs)
    D = np.zeros((n,n))

    corrected_vdj_dict.update({it:{}})
    # if multiple VDJ sequences, get distances
    if n > 1:
        for i in range(n):
            for j in range(i):
                d = distance(unique_vdj_seqs[i],unique_vdj_seqs[j])
                D[i,j] = d
                D[j,i] = d

        # first cut this into connected components, 
        # max distance between connected components is MAX_ERROR_IN_VDJ
        graph = csr_matrix(D < MAX_ERROR_IN_VDJ)
        n_components, labels = connected_components(csgraph=graph, directed=False, return_labels=True)
        
        labels = np.asarray(labels)
        umi_support_per_component = np.zeros(n_components)
        for i in range(n_components):
            umi_support_per_component[i] = (umis_per_seq[labels == i]).sum()

        # keep only components supported by multiple UMIs, and no fewer than 10% of droplet content
        # if more than one connected component require at least 4 UMIs
        UMI_support_threshold = 1 + 2 * int(n_components > 1)
        fractional_umi_support = umi_support_per_component/umi_support_per_component.sum()
        good_components = np.arange(n_components)[(fractional_umi_support > MIN_DROPLET_FRACTION) \
                          & (umi_support_per_component > UMI_support_threshold)]
        components.append((len(good_components), n_components))
        count += 1

        if len(good_components) > 0:
            is_cell_dict[it] = True

            if len(good_components) > 1:
                is_multiplet_dict[it] = True
            else:
                is_multiplet_dict[it] = False
            
            cell_vdj_calls = []

            for good_component in good_components:
                good_component_locs = np.arange(len(labels))[labels == good_component]
                D_subset = D[np.ix_(good_component_locs, good_component_locs)]
                umi_support_subset = umis_per_seq[np.ix_(good_component_locs)]

                #calculate total number of mutations needed to explain data
                C = (D_subset*umi_support_subset).sum(axis=1)

                best_seq_idx = good_component_locs[np.argmin(C)]
                best_seq = unique_vdj_seqs[best_seq_idx]
                
                cell_vdj_calls.append(best_seq)

                for cindex in good_component_locs:
                    corrected_vdj_dict[it].update({unique_vdj_seqs[cindex] : best_seq})

            vdj_call_dict[it] = cell_vdj_calls

        else:
            is_cell_dict[it] = False
            is_multiplet_dict[it] = False
            vdj_call_dict[it] = None

    else:
        if max_umi_support > 1:
            is_cell_dict[it] = True
            is_multiplet_dict[it] = False
            vdj_call_dict[it] = [unique_vdj_seqs[-1]]
            corrected_vdj_dict[it][unique_vdj_seqs[-1]] = unique_vdj_seqs[-1]
        else:
            is_cell_dict[it] = False
            is_multiplet_dict[it] = False
            vdj_call_dict[it] = None


cb_df['is_cell'] = cb_df.index.map(lambda x: is_cell_dict[x]).astype(bool)
cb_df['is_multiplet'] = cb_df.index.map(lambda x: is_multiplet_dict[x]).astype(bool)
cb_df['vdj_calls'] = cb_df.index.map(lambda x: vdj_call_dict[x])
cb_df['multiplet'] = cb_df.vdj_calls.str.len()


print("INFO: total droplets per sample:", file=sys.stderr)
print(cb_df.groupby('sample_id')['is_cell'].agg(sum), file=sys.stderr)

print("INFO: total cells per sample:", file=sys.stderr)
print(cb_df.groupby('sample_id')['multiplet'].agg(sum), file=sys.stderr)

print("INFO: Multiplet distribution:", file=sys.stderr)

multiplet_distribution = cb_df.groupby(['sample_id','multiplet']).size().reset_index()

multiplet_distribution = multiplet_distribution.pivot(index='multiplet', 
                                                      columns='sample_id', 
                                                      values=0)

multiplet_distribution = multiplet_distribution/ multiplet_distribution.sum(axis=0)

print(multiplet_distribution, file=sys.stderr)


ambient_droplets = deepcopy(cb_df[(~cb_df['is_cell'].astype(bool))])
ambient_droplets['vdj_c_umi_r'] = ambient_droplets.apply(lambda x: [(x.vdj_sequence[i], 
                                                                  x.c_call[i], 
                                                                  x.umi[i], 
                                                                  str(x.reads[i])) for i in range(len(x.reads))
                                                                 ], axis=1)


ambient_droplets['droplet_id'] = ambient_droplets['sample_id'] + "+" + ambient_droplets['cb']
ambient_droplets = ambient_droplets[['droplet_id', 'vdj_c_umi_r']]
ambient_droplets = ambient_droplets.explode('vdj_c_umi_r')

called_cells = deepcopy(cb_df[cb_df['is_cell']])

ambient_vdj_dict = {}

cell_vdj_dict = {}
cell_c_call_dict = {}
cell_umi_count_dict = {}
cell_read_count_dict = {}

# for each called cell, assign isotype and UMIs
# record ambient RNA vdjs
total_vdj_calls = cb_df.vdj_calls.str.len().sum()

unique_c_call = 0
majority_c_call_possible = 0
majority_c_call = []
second_best_to_majority = []

otherwise = 0

umi_uncorrected = []
umi_corrected = []

Ds = []
Cs = []

for it, row in called_cells.iterrows():
    # first record ambient RNA

    droplet_id = row.sample_id + "_" + row.cb

    ambient_vdj_info = [(row.vdj_sequence[it2], 
                         row.c_call[it2],
                         row.umi[it2],
                         row.reads[it2])

                        for it2 in range(len(row.vdj_sequence))
                            if (corrected_vdj_dict[it].get(row.vdj_sequence[it2],None) is None)]
    droplet_ids = [droplet_id]*len(ambient_vdj_info)

    ambient_droplets.append(pd.DataFrame({'droplet_id':droplet_ids, 
                                 'vdj_c_umi_r':ambient_vdj_info}))

    corrected_vdj_info = [(corrected_vdj_dict[it][row.vdj_sequence[it2]], 
                           row.umi[it2],
                           row.c_call[it2],
                           row.reads[it2])

                        for it2 in range(len(row.vdj_sequence))
                            if not (corrected_vdj_dict[it].get(row.vdj_sequence[it2], None) is None)]

    # first collapse reads made collapsible by vdj error-correction
    cell_dict = {vdj:{} for vdj in row.vdj_calls}
    cell_umi_count_dict[it] = {}
    cell_read_count_dict[it] = {}
    cell_c_call_dict[it] = {}

    #find consensus C gene call

    for vdj, umi, c_call, r in corrected_vdj_info:
        cell_dict[vdj][c_call] = {}

    for vdj, umi, c_call, r in corrected_vdj_info:
        cell_dict[vdj][c_call][umi] = cell_dict[vdj][c_call].get(umi, 0) + int(r)

    for vdj, info in cell_dict.items():
        cell_umi_count_dict[it].update({vdj:{}})

        # first check whether c_call is unique
        if len(info.keys()) > 1:

            #if not check whether best-supported C call 
            #is supported by at least twice the number of UMIs as second-best C call

            umis_per_c_call = {c_call: len(info[c_call].keys()) 
                                for c_call in info.keys()}

            total_umis = sum(umis_per_c_call.values())
            fractional_umis_per_c_call = sorted((umis_per_c_call[c_call]/total_umis, c_call)
                                            for c_call in umis_per_c_call.keys())
            best_c_call = fractional_umis_per_c_call[-1]
            second_best_c_call = fractional_umis_per_c_call[-2]

            if (second_best_c_call[0] / best_c_call[0] <= 0.5):
                majority_c_call_possible += 1
                c_call_possible = True
                c_call = best_c_call[-1]

            else:
                c_call_possible = False
                otherwise += 1
                c_call = 'ambiguous'

            majority_c_call.append(best_c_call[0])

            second_best_to_majority.append(second_best_c_call[0]/best_c_call[0])

        else:
            c_call_possible = True
            c_call = list(info.keys())[0]
            unique_c_call += 1

        cell_c_call_dict[it].update({vdj: c_call})

        #collect info about umi and read numbers for this cell
        umis = {}
        for c_call in info.keys():
            for umi, reads in info[c_call].items():
                umis[umi] = umis.get(umi,0) + reads

        unique_umis = list(umis.keys())
        reads_per_umi = np.asarray([umis[u] for u in unique_umis])
        total_reads = reads_per_umi.sum()

        cell_umi_count_dict[it][vdj] = len(unique_umis)
        cell_read_count_dict[it][vdj] = total_reads


print("Unique C gene call: %d" % unique_c_call, file=sys.stderr)
print("Majority C call: %d" % majority_c_call_possible, file=sys.stderr)
print("Total confident C calls: {}".format((unique_c_call + majority_c_call_possible)), file=sys.stderr)
print("Total cells called: {}".format((unique_c_call + majority_c_call_possible + otherwise)), file=sys.stderr)

called_cells['consensus_c_call'] = called_cells.index.map(lambda x: cell_c_call_dict[x])
called_cells['n_umis'] = called_cells.index.map(lambda x: cell_umi_count_dict[x])
called_cells['n_reads'] = called_cells.index.map(lambda x: cell_read_count_dict[x])

called_cells['vdjc_info'] = called_cells.apply(lambda x: [(y, x.consensus_c_call[y], x.n_umis[y], x.n_reads[y]) 
                                                            for y in x.vdj_calls], axis=1)

called_cells = called_cells[['sample_id', 'cb', 'vdjc_info']]
called_cells = called_cells.explode('vdjc_info')
called_cells['vdj_call'] = called_cells.vdjc_info.map(lambda x: x[0])
called_cells['c_call'] = called_cells.vdjc_info.map(lambda x: x[1])
called_cells['n_umis'] = called_cells.vdjc_info.map(lambda x: x[2])
called_cells['n_reads'] = called_cells.vdjc_info.map(lambda x: x[3])

called_cells.to_csv('{}/{}_called_cells.tsv.gz'.format(OUTDIR, OUTNAME), sep='\t', index=False)

print('Cell calling done!', file=sys.stderr)


print('Compiling information about ambient VDJ sequences...', file=sys.stderr)

ambient_droplets['vdj_sequence'] = ambient_droplets.vdj_c_umi_r.map(lambda x: x[0])
ambient_droplets['c_call'] = ambient_droplets.vdj_c_umi_r.map(lambda x: x[1])
ambient_droplets['umi'] = ambient_droplets.vdj_c_umi_r.map(lambda x: x[2])
ambient_droplets['reads'] = ambient_droplets.vdj_c_umi_r.map(lambda x: int(x[3]))

ambient_droplets['sample_id'] = ambient_droplets.droplet_id.map(lambda x: x.split("+")[0])
ambient_droplets['cb'] = ambient_droplets.droplet_id.map(lambda x: x.split("+")[1])


ambient_vdjs = ambient_droplets.groupby(['vdj_sequence', 'c_call', 'sample_id']).agg(list)
ambient_vdjs = ambient_vdjs.reset_index()

ambient_vdjs['n_droplets'] = ambient_vdjs.cb.map(lambda x: len(set(x)))
ambient_vdjs['n_umis'] = ambient_vdjs.umi.str.len()
ambient_vdjs['n_reads'] = ambient_vdjs.reads.map(lambda x: sum(x))
ambient_vdjs['umis_per_droplet'] = ambient_vdjs.cb.map(lambda x: ",".join(["{}_{}".format(y, x.count(y))
                                                                            for y in set(x)]))

ambient_vdjs['umi'] = ambient_vdjs.umi.str.join(",")
ambient_vdjs['cb'] = ambient_vdjs.cb.str.join(",")
ambient_vdjs['reads'] = ambient_vdjs.reads.map(lambda x: ",".join([str(y) for y in x]))


ambient_vdjs.to_csv('{}/{}_ambient_vdjs.tsv.gz'.format(OUTDIR, OUTNAME), sep='\t', index=False)